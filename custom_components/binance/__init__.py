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
import time
from datetime import timedelta
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
    PNL_DATA,
    PLATFORMS,
    RATE_LIMIT_BACKOFF_BASE,
    RATE_LIMIT_BACKOFF_MAX,
    SHARED_KEY,
    SPOT_API_URL,
    SPOT_DATA,
    WALLET_DATA,
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

                futures_data = (
                    {i["symbol"]: i for i in fetched["futures"]}
                    if "futures" in fetched
                    and not isinstance(fetched["futures"], Exception)
                    else existing.get(FUTURES_DATA, {})
                )
                spot_data = (
                    {i["symbol"]: i for i in fetched["spot"]}
                    if "spot" in fetched
                    and not isinstance(fetched["spot"], Exception)
                    else existing.get(SPOT_DATA, {})
                )
                btcusdt = (
                    float(fetched["btcusdt"]["price"])
                    if not isinstance(fetched.get("btcusdt"), Exception)
                    else existing.get(BTCUSDT_PRICE)
                )

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
                )
                pnl_task = _request(
                    self.session,
                    f"{FUTURES_API_URL}/fapi/v2/positionRisk",
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    signed=True,
                )

                wallet_raw, pnl_raw = await asyncio.gather(
                    wallet_task, pnl_task, return_exceptions=True
                )

                existing = self.data or {}

                # Wallet
                if isinstance(wallet_raw, Exception):
                    _LOGGER.warning("Wallet fetch failed: %s", wallet_raw)
                    wallet_data = existing.get(WALLET_DATA, {})
                else:
                    wallet_data = {
                        item["walletName"]: float(item["balance"])
                        for item in wallet_raw
                    }

                # PnL — keep only open positions
                if isinstance(pnl_raw, Exception):
                    _LOGGER.warning("PnL fetch failed: %s", pnl_raw)
                    pnl_data = existing.get(PNL_DATA, [])
                else:
                    pnl_data = [
                        {
                            "symbol": p["symbol"],
                            "positionAmt": float(p["positionAmt"]),
                            "entryPrice": float(p["entryPrice"]),
                            "markPrice": float(p["markPrice"]),
                            "unRealizedProfit": float(p["unRealizedProfit"]),
                            "liquidationPrice": float(
                                p.get("liquidationPrice", 0)
                            ),
                            "leverage": int(p.get("leverage", 1)),
                            "marginType": p.get("marginType", "cross"),
                            "positionSide": p.get("positionSide", "BOTH"),
                        }
                        for p in pnl_raw
                        if float(p.get("positionAmt", 0)) != 0
                    ]

                return {WALLET_DATA: wallet_data, PNL_DATA: pnl_data}

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
