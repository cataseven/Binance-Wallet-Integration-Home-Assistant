"""The Binance integration.

Architecture
------------
- **Shared price coordinator** (one per HA instance):
  Fetches all spot/futures ticker data + BTCUSDT reference price.
  No authentication required.  Shared across all config entries so that
  "Binance Futures Market" and "Binance Spot Market" devices appear once.
  WebSocket streams are also managed here.

- **Per-account coordinator** (one per config entry):
  Fetches wallet balances and futures PnL using the entry's API key/secret.

hass.data[DOMAIN] layout:
    "_shared": {
        "price_coordinator": BinancePriceCoordinator,
        "ws_manager": BinanceWebSocketManager | None,
        "pair_registry": {entry_id: {"futures": [...], "spot": [...]}, ...},
        "use_websocket": bool,
    }
    entry_id: {
        "account_coordinator": BinanceAccountCoordinator,
    }
"""

import asyncio
import hashlib
import hmac
import logging
import math
import time
from datetime import timedelta
from functools import partial
from http import HTTPStatus

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BTCUSDT_PRICE,
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_FUTURES_PAIRS,
    CONF_SPOT_PAIRS,
    CONF_UPDATE_INTERVAL,
    CONF_USE_WEBSOCKET,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_USE_WEBSOCKET,
    DOMAIN,
    FUTURES_API_URL,
    FUTURES_DATA,
    MARGIN_DATA,
    PNL_DATA,
    PLATFORMS,
    RATE_LIMIT_BACKOFF_BASE,
    RATE_LIMIT_BACKOFF_MAX,
    SHARED_KEY,
    SPOT_API_URL,
    SPOT_DATA,
    WALLET_DATA,
    WALLET_USD_DATA,
)
from .websocket import BinanceWebSocketManager

_LOGGER = logging.getLogger(__name__)


# ======================================================================
# Helpers
# ======================================================================


def _get_entry_pairs(entry: ConfigEntry) -> tuple[list[str], list[str]]:
    """Return (futures_pairs, spot_pairs) for a config entry."""
    futures = entry.options.get(
        CONF_FUTURES_PAIRS, entry.data.get(CONF_FUTURES_PAIRS, [])
    )
    spot = entry.options.get(
        CONF_SPOT_PAIRS, entry.data.get(CONF_SPOT_PAIRS, [])
    )
    return list(futures), list(spot)


def _merged_pairs(shared: dict) -> tuple[list[str], list[str]]:
    """Compute the union of all entries' pair lists."""
    all_futures: set[str] = set()
    all_spot: set[str] = set()
    for pairs in shared["pair_registry"].values():
        all_futures.update(pairs.get("futures", []))
        all_spot.update(pairs.get("spot", []))
    return sorted(all_futures), sorted(all_spot)


async def _request(
    session: aiohttp.ClientSession,
    url: str,
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    signed: bool = False,
    params: dict | None = None,
) -> list | dict:
    """GET request with optional HMAC signing and rate-limit detection."""
    headers: dict[str, str] = {}
    if signed and api_key and api_secret:
        headers["X-MBX-APIKEY"] = api_key
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 10000
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        params["signature"] = hmac.new(
            api_secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()

    async with session.get(url, headers=headers, params=params) as resp:
        if resp.status in (HTTPStatus.TOO_MANY_REQUESTS, 418):
            retry = int(resp.headers.get("Retry-After", RATE_LIMIT_BACKOFF_BASE))
            raise UpdateFailed(
                f"Binance rate limit (HTTP {resp.status}), back off {retry}s"
            )
        resp.raise_for_status()
        return await resp.json()


# What a malformed payload raises while being parsed. Transport errors are
# separate: asyncio.gather(return_exceptions=True) hands those back as values.
_PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError)


def _to_float(value, default: float | None = 0.0) -> float | None:
    """float() for Binance's string-encoded numbers.

    Some accounts receive "" instead of a number (seen for every balance
    field of /fapi/v2/account), so a bare float() would abort the refresh.
    """
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _parse_section(failed: set[str], section: str, parser, raw, fallback):
    """Parse one endpoint's payload; keep *fallback* if it is malformed.

    Sections are independent, so an unexpected futures payload cannot take
    the wallet sensors down with it. Logs once per failure streak.
    """
    try:
        result = parser(raw)
    except _PARSE_ERRORS as err:
        if section not in failed:
            failed.add(section)
            _LOGGER.warning(
                "Unexpected %s response from Binance, keeping previous data: %r",
                section,
                err,
            )
        return fallback
    if section in failed:
        failed.discard(section)
        _LOGGER.info("Binance %s data recovered", section)
    return result


def _index_by_symbol(raw: list) -> dict[str, dict]:
    """Ticker list → {symbol: ticker}."""
    return {item["symbol"]: item for item in raw}


def _parse_wallets(raw: list, previous: dict) -> dict[str, float | None]:
    """walletName → balance.

    A blank balance keeps the wallet's previous value instead of reading as
    0, which would record a false balance drop and fire automations.
    """
    wallets: dict[str, float | None] = {}
    for item in raw:
        name = item["walletName"]
        balance = _to_float(item.get("balance"), None)
        wallets[name] = balance if balance is not None else previous.get(name)
    return wallets


def _parse_positions(raw: list) -> list[dict]:
    """Open positions from /fapi/v2/positionRisk."""
    positions = []
    for p in raw:
        amount = _to_float(p.get("positionAmt"))
        if not amount:
            continue
        positions.append(
            {
                "symbol": p["symbol"],
                "positionAmt": amount,
                "entryPrice": _to_float(p.get("entryPrice")),
                "markPrice": _to_float(p.get("markPrice")),
                "unRealizedProfit": _to_float(p.get("unRealizedProfit")),
                "liquidationPrice": _to_float(p.get("liquidationPrice")),
                "leverage": int(_to_float(p.get("leverage"), 1) or 1),
                "marginType": p.get("marginType", "cross"),
                "positionSide": p.get("positionSide", "BOTH"),
            }
        )
    return positions


def _margin_ratio(maint_margin: float, margin_balance: float) -> float | None:
    """Margin ratio in % — Binance liquidates when it reaches 100."""
    if maint_margin == 0:
        return 0.0
    if margin_balance <= 0:
        return None
    return round(maint_margin / margin_balance * 100, 2)


def _build_margin_data(account: dict) -> dict:
    """Extract margin-ratio data from a /fapi/v2/account response.

    Cross positions share one account-wide ratio (cross maintenance margin
    over cross margin balance); isolated positions each carry their own,
    computed from the position's isolated wallet + unrealized PnL. This
    mirrors the ratios Binance shows in the Futures UI.
    """
    # Indexed, not .get(): an error body must raise rather than read as an
    # empty account. Blank values, as some accounts receive, parse as 0.
    cross_margin_balance = _to_float(
        account["totalCrossWalletBalance"]
    ) + _to_float(account.get("totalCrossUnPnl"))

    cross_maint = 0.0
    positions: dict[str, dict] = {}

    for pos in account.get("positions") or []:
        if not _to_float(pos.get("positionAmt")):
            continue
        maint = _to_float(pos.get("maintMargin"))
        key = f"{pos['symbol']}:{pos.get('positionSide', 'BOTH')}"
        if pos.get("isolated"):
            iso_balance = _to_float(pos.get("isolatedWallet")) + _to_float(
                pos.get("unrealizedProfit")
            )
            positions[key] = {
                "margin_type": "isolated",
                "maint_margin": maint,
                "margin_balance": iso_balance,
                "margin_ratio": _margin_ratio(maint, iso_balance),
            }
        else:
            cross_maint += maint
            positions[key] = {
                "margin_type": "cross",
                "maint_margin": maint,
                # Filled in below once cross_maint is fully summed.
                "margin_balance": None,
                "margin_ratio": None,
            }

    cross_ratio = _margin_ratio(cross_maint, cross_margin_balance)
    for detail in positions.values():
        if detail["margin_type"] == "cross":
            detail["margin_balance"] = cross_margin_balance
            detail["margin_ratio"] = cross_ratio

    total_maint = _to_float(account.get("totalMaintMargin"))
    total_balance = _to_float(account.get("totalMarginBalance"))

    return {
        "margin_ratio": cross_ratio,
        "maint_margin": cross_maint,
        "margin_balance": cross_margin_balance,
        "total_margin_ratio": _margin_ratio(total_maint, total_balance),
        "total_maint_margin": total_maint,
        "total_margin_balance": total_balance,
        "available_balance": _to_float(account.get("availableBalance")),
        "positions": positions,
    }


# ======================================================================
# Shared Price Coordinator
# ======================================================================


class BinancePriceCoordinator(DataUpdateCoordinator):
    """Fetches public price data shared across all accounts."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        update_interval: int,
        use_websocket: bool,
    ) -> None:
        self.session = session
        self.use_websocket = use_websocket
        self._backoff_until: float = 0
        self._failed_sections: set[str] = set()

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_price",
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self) -> dict:
        remaining = self._backoff_until - time.monotonic()
        if remaining > 0:
            raise UpdateFailed(f"Rate-limit backoff, {remaining:.0f}s left")

        try:
            async with asyncio.timeout(30):
                tasks: dict[str, any] = {}

                # Always REST on first load; afterwards skip if WS is active.
                need_rest = not self.use_websocket or self.data is None
                if need_rest:
                    tasks["futures"] = _request(
                        self.session, f"{FUTURES_API_URL}/fapi/v1/ticker/24hr"
                    )
                    tasks["spot"] = _request(
                        self.session, f"{SPOT_API_URL}/api/v3/ticker/24hr"
                    )

                tasks["btcusdt"] = _request(
                    self.session,
                    f"{SPOT_API_URL}/api/v3/ticker/price",
                    params={"symbol": "BTCUSDT"},
                )

                keys = list(tasks.keys())
                results = await asyncio.gather(
                    *tasks.values(), return_exceptions=True
                )
                fetched = dict(zip(keys, results))

                for k, v in fetched.items():
                    if isinstance(v, Exception):
                        _LOGGER.warning("Price fetch %s failed: %s", k, v)

                existing = self.data or {}

                futures_data = existing.get(FUTURES_DATA, {})
                if "futures" in fetched and not isinstance(
                    fetched["futures"], Exception
                ):
                    futures_data = _parse_section(
                        self._failed_sections, "futures ticker",
                        _index_by_symbol, fetched["futures"], futures_data,
                    )

                spot_data = existing.get(SPOT_DATA, {})
                if "spot" in fetched and not isinstance(
                    fetched["spot"], Exception
                ):
                    spot_data = _parse_section(
                        self._failed_sections, "spot ticker",
                        _index_by_symbol, fetched["spot"], spot_data,
                    )

                btcusdt = existing.get(BTCUSDT_PRICE)
                if not isinstance(fetched["btcusdt"], Exception):
                    price = _parse_section(
                        self._failed_sections, "BTCUSDT price",
                        lambda raw: _to_float(raw["price"], None),
                        fetched["btcusdt"], None,
                    )
                    if price:
                        btcusdt = price

                return {
                    FUTURES_DATA: futures_data,
                    SPOT_DATA: spot_data,
                    BTCUSDT_PRICE: btcusdt,
                }

        except UpdateFailed:
            raise
        except aiohttp.ClientResponseError as err:
            raise UpdateFailed(f"API error {err.status}: {err.message}") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Connection error: {err}") from err
        except TimeoutError as err:
            raise UpdateFailed("Request timed out") from err


# ======================================================================
# Per-Account Coordinator
# ======================================================================


class BinanceAccountCoordinator(DataUpdateCoordinator):
    """Fetches authenticated per-account data (wallets, PnL)."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        api_key: str,
        api_secret: str,
        update_interval: int,
    ) -> None:
        self.session = session
        self.api_key = api_key
        self.api_secret = api_secret
        self._backoff_until: float = 0
        self._failed_sections: set[str] = set()

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_account",
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self) -> dict:
        remaining = self._backoff_until - time.monotonic()
        if remaining > 0:
            raise UpdateFailed(f"Rate-limit backoff, {remaining:.0f}s left")

        try:
            async with asyncio.timeout(30):
                wallet_task = _request(
                    self.session,
                    f"{SPOT_API_URL}/sapi/v1/asset/wallet/balance",
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    signed=True,
                    params={"quoteAsset": "BTC"},
                )
                # Same endpoint valued directly in USDT by Binance — the
                # USD wallet sensors read this instead of converting the
                # BTC figure with a locally cached BTCUSDT price.
                wallet_usd_task = _request(
                    self.session,
                    f"{SPOT_API_URL}/sapi/v1/asset/wallet/balance",
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    signed=True,
                    params={"quoteAsset": "USDT"},
                )
                pnl_task = _request(
                    self.session,
                    f"{FUTURES_API_URL}/fapi/v2/positionRisk",
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    signed=True,
                )
                account_task = _request(
                    self.session,
                    f"{FUTURES_API_URL}/fapi/v2/account",
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    signed=True,
                )

                wallet_raw, wallet_usd_raw, pnl_raw, account_raw = (
                    await asyncio.gather(
                        wallet_task, wallet_usd_task, pnl_task, account_task,
                        return_exceptions=True,
                    )
                )

                existing = self.data or {}

                failed = self._failed_sections

                # Wallet (BTC valuation)
                wallet_data = existing.get(WALLET_DATA, {})
                if isinstance(wallet_raw, Exception):
                    _LOGGER.warning("Wallet fetch failed: %s", wallet_raw)
                else:
                    wallet_data = _parse_section(
                        failed, "wallet balance",
                        partial(_parse_wallets, previous=wallet_data),
                        wallet_raw, wallet_data,
                    )

                # Wallet (USDT valuation)
                wallet_usd_data = existing.get(WALLET_USD_DATA, {})
                if isinstance(wallet_usd_raw, Exception):
                    _LOGGER.warning(
                        "Wallet USD fetch failed: %s", wallet_usd_raw
                    )
                else:
                    wallet_usd_data = _parse_section(
                        failed, "wallet USD balance",
                        partial(_parse_wallets, previous=wallet_usd_data),
                        wallet_usd_raw, wallet_usd_data,
                    )

                # PnL — keep only open positions
                pnl_data = existing.get(PNL_DATA, [])
                if isinstance(pnl_raw, Exception):
                    _LOGGER.warning("PnL fetch failed: %s", pnl_raw)
                else:
                    pnl_data = _parse_section(
                        failed, "futures position",
                        _parse_positions, pnl_raw, pnl_data,
                    )

                # Margin — account-level + per-position margin ratios
                margin_data = existing.get(MARGIN_DATA, {})
                if isinstance(account_raw, Exception):
                    _LOGGER.warning(
                        "Futures account fetch failed: %s", account_raw
                    )
                else:
                    margin_data = _parse_section(
                        failed, "futures account",
                        _build_margin_data, account_raw, margin_data,
                    )

                return {
                    WALLET_DATA: wallet_data,
                    WALLET_USD_DATA: wallet_usd_data,
                    PNL_DATA: pnl_data,
                    MARGIN_DATA: margin_data,
                }

        except UpdateFailed:
            raise
        except aiohttp.ClientResponseError as err:
            raise UpdateFailed(f"API error {err.status}: {err.message}") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Connection error: {err}") from err
        except TimeoutError as err:
            raise UpdateFailed("Request timed out") from err


# ======================================================================
# Shared layer management
# ======================================================================


async def _ensure_shared(
    hass: HomeAssistant,
    entry: ConfigEntry,
    futures_pairs: list[str],
    spot_pairs: list[str],
) -> dict:
    """Create or update the shared price coordinator and WebSocket manager."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    session = async_get_clientsession(hass)
    use_ws = entry.options.get(CONF_USE_WEBSOCKET, DEFAULT_USE_WEBSOCKET)
    interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

    shared = domain_data.get(SHARED_KEY)

    if shared is None:
        # First entry — bootstrap shared layer.
        coordinator = BinancePriceCoordinator(
            hass, session, interval, use_ws,
        )
        shared = {
            "price_coordinator": coordinator,
            "ws_manager": None,
            "pair_registry": {},
            "use_websocket": use_ws,
        }
        domain_data[SHARED_KEY] = shared

        await coordinator.async_config_entry_first_refresh()

    # Register this entry's pairs.
    shared["pair_registry"][entry.entry_id] = {
        "futures": futures_pairs,
        "spot": spot_pairs,
    }

    # Restart WebSocket with merged pairs.
    await _refresh_websocket(hass)

    return shared


async def _refresh_websocket(hass: HomeAssistant) -> None:
    """(Re)start WebSocket with the union of all registered pairs."""
    shared = hass.data[DOMAIN].get(SHARED_KEY)
    if not shared:
        return

    ws: BinanceWebSocketManager | None = shared.get("ws_manager")
    use_ws = shared.get("use_websocket", False)
    all_futures, all_spot = _merged_pairs(shared)

    if use_ws and (all_futures or all_spot):
        session = async_get_clientsession(hass)
        coordinator = shared["price_coordinator"]

        if ws is None:
            ws = BinanceWebSocketManager(hass, coordinator, session)
            shared["ws_manager"] = ws
        else:
            await ws.stop()

        await ws.start(all_spot, all_futures)
    elif ws:
        await ws.stop()
        shared["ws_manager"] = None


async def _unregister_shared(hass: HomeAssistant, entry_id: str) -> None:
    """Remove an entry from the shared layer; tear down if last."""
    shared = hass.data[DOMAIN].get(SHARED_KEY)
    if not shared:
        return

    shared["pair_registry"].pop(entry_id, None)

    if not shared["pair_registry"]:
        # Last entry — tear down.
        ws: BinanceWebSocketManager | None = shared.get("ws_manager")
        if ws:
            await ws.stop()
        hass.data[DOMAIN].pop(SHARED_KEY, None)
    else:
        await _refresh_websocket(hass)


# ======================================================================
# Entry setup / unload
# ======================================================================


async def _options_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _LOGGER.info("Binance options updated, reloading integration")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Binance from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    session = async_get_clientsession(hass)

    futures_pairs, spot_pairs = _get_entry_pairs(entry)
    interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

    # --- Shared price coordinator ---
    shared = await _ensure_shared(hass, entry, futures_pairs, spot_pairs)

    # --- Per-account coordinator ---
    account_coordinator = BinanceAccountCoordinator(
        hass,
        session,
        api_key=entry.data[CONF_API_KEY],
        api_secret=entry.data[CONF_API_SECRET],
        update_interval=interval,
    )
    await account_coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "account_coordinator": account_coordinator,
    }

    entry.async_on_unload(entry.add_update_listener(_options_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await _unregister_shared(hass, entry.entry_id)
    return unload_ok
