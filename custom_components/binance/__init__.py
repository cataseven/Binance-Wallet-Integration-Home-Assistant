"""The Binance integration.

Architecture
------------
- **Shared price coordinator** (one per HA instance):
  Fetches all spot/futures ticker data + BTCUSDT reference price.
  No authentication required.  Shared across all config entries so that
  "Binance Futures Market" and "Binance Spot Market" devices appear once.
  WebSocket streams are also managed here.

- **Shared market coordinator** (one per HA instance):
  Kline reference prices for the multi-timeframe % changes and futures
  funding data, for the pairs of entries that enabled those features.

- **Per-account coordinator** (one per config entry):
  Fetches wallet balances, futures positions and margin using the entry's
  API key/secret.

hass.data[DOMAIN] layout:
    "_shared": {
        "price_coordinator": BinancePriceCoordinator,
        "market_coordinator": BinanceMarketCoordinator,
        "ws_manager": BinanceWebSocketManager | None,
        "pair_registry": {
            entry_id: {"futures": [...], "spot": [...],
                       "changes": bool, "funding": bool},
            ...
        },
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
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BTCUSDT_PRICE,
    CHANGE_REFS,
    CONF_ACCOUNT_NAME,
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_FUNDING_SENSORS,
    CONF_FUTURES_PAIRS,
    CONF_PRICE_CHANGES,
    CONF_SPOT_PAIRS,
    CONF_UPDATE_INTERVAL,
    CONF_USE_WEBSOCKET,
    DEFAULT_FUNDING_INTERVAL_HOURS,
    DEFAULT_FUNDING_SENSORS,
    DEFAULT_PRICE_CHANGES,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_USE_WEBSOCKET,
    DOMAIN,
    FUNDING_DATA,
    FUNDING_INFO_REFRESH,
    FUTURES_API_URL,
    FUTURES_DATA,
    HOUR_WINDOWS,
    HOURLY_KLINES_REFRESH,
    KLINE_CONCURRENCY,
    MARGIN_DATA,
    MARKET_UPDATE_INTERVAL,
    MINUTE_WINDOWS,
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


class BinanceRateLimited(UpdateFailed):
    """HTTP 429 / 418 from Binance, with the server-requested wait."""

    def __init__(self, status: int, retry_after: int) -> None:
        super().__init__(
            f"Binance rate limit (HTTP {status}), back off {retry_after}s"
        )
        self.retry_after = retry_after


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
            retry = _to_float(resp.headers.get("Retry-After"), None)
            raise BinanceRateLimited(
                resp.status,
                int(retry) if retry and retry > 0 else RATE_LIMIT_BACKOFF_BASE,
            )
        resp.raise_for_status()
        return await resp.json()


# What a malformed payload raises while being parsed. Transport errors are
# separate: asyncio.gather(return_exceptions=True) hands those back as values.
_PARSE_ERRORS = (LookupError, TypeError, ValueError, AttributeError)


def _to_float(value, default: float | None = 0.0) -> float | None:
    """float() for Binance's string-encoded numbers.

    Some accounts receive "" instead of a number (seen for every balance
    field of /fapi/v2/account), so a bare float() would abort the refresh.
    """
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _parse_section(
    failed: set[str], source: str, section: str, parser, raw, fallback
):
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
                "Unexpected %s response from Binance for %s, keeping previous "
                "data: %r",
                section,
                source,
                err,
            )
        return fallback
    if section in failed:
        failed.discard(section)
        _LOGGER.info("Binance %s data recovered for %s", section, source)
    return result


def _index_by_symbol(raw: list) -> dict[str, dict]:
    """Ticker list → {symbol: ticker}."""
    return {item["symbol"]: item for item in raw}


def _parse_price(raw: dict) -> float:
    """BTCUSDT reference price; anything but a positive number raises."""
    price = float(raw["price"])
    if not (math.isfinite(price) and price > 0):
        raise ValueError(f"invalid price {raw['price']!r}")
    return price


def _parse_wallets(
    raw: list, previous: dict, failed: set[str], source: str, section: str
) -> dict[str, float | None]:
    """walletName → balance.

    A blank balance keeps the wallet's previous value instead of reading as
    0, which would record a false balance drop and fire automations. Logged
    once per blank streak so the stale value is not silent.
    """
    wallets: dict[str, float | None] = {}
    for item in raw:
        name = item["walletName"]
        balance = _to_float(item.get("balance"), None)
        key = f"{section}: {name}"
        if balance is None:
            balance = previous.get(name)
            if key not in failed:
                failed.add(key)
                _LOGGER.warning(
                    "Blank %s for wallet %s from Binance for %s, keeping the "
                    "previous value",
                    section,
                    name,
                    source,
                )
        elif key in failed:
            failed.discard(key)
            _LOGGER.info(
                "Binance %s for wallet %s recovered for %s", section, name, source
            )
        wallets[name] = balance
    return wallets


def _parse_positions(raw: list) -> list[dict]:
    """Open positions from /fapi/v2/positionRisk.

    positionAmt and unRealizedProfit stay strict: a blank one must not read
    as a closed position or a 0 PnL, so the section keeps its previous data.
    """
    positions = []
    for p in raw:
        amount = float(p["positionAmt"])
        if amount == 0:
            continue
        positions.append(
            {
                "symbol": p["symbol"],
                "positionAmt": amount,
                "entryPrice": _to_float(p.get("entryPrice")),
                "markPrice": _to_float(p.get("markPrice")),
                "unRealizedProfit": float(p["unRealizedProfit"]),
                "liquidationPrice": _to_float(p.get("liquidationPrice")),
                "leverage": int(_to_float(p.get("leverage"), 1) or 1),
                "marginType": p.get("marginType", "cross"),
                "positionSide": p.get("positionSide", "BOTH"),
            }
        )
    return positions


def _margin_ratio(
    maint_margin: float | None, margin_balance: float | None
) -> float | None:
    """Margin ratio in % — Binance liquidates when it reaches 100."""
    if maint_margin is None or margin_balance is None:
        return None
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
    # empty account. Blank is different: Binance sends "" in every numeric
    # field when the login has no USDⓈ-M Futures account (a real zero is
    # "0.00000000"), so the ratios are unknown there, not 0 %.
    cross_wallet = _to_float(account["totalCrossWalletBalance"], None)
    cross_unpnl = _to_float(account.get("totalCrossUnPnl"), None)
    cross_margin_balance = (
        None
        if cross_wallet is None or cross_unpnl is None
        else cross_wallet + cross_unpnl
    )

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

    total_maint = _to_float(account.get("totalMaintMargin"), None)
    total_balance = _to_float(account.get("totalMarginBalance"), None)

    return {
        "futures_enabled": cross_wallet is not None,
        "margin_ratio": cross_ratio,
        "maint_margin": cross_maint,
        "margin_balance": cross_margin_balance,
        "total_margin_ratio": _margin_ratio(total_maint, total_balance),
        "total_maint_margin": total_maint,
        "total_margin_balance": total_balance,
        "available_balance": _to_float(account.get("availableBalance"), None),
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
            # Shared by every account: left unbound, otherwise HA ties it to
            # whichever entry happened to create it and shuts it down when
            # that one entry reloads or fails.
            config_entry=None,
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
                existing = self.data or {}

                # REST until a ticker list has loaded; WS keeps it current
                # after that, but only updates symbols already in the list.
                if not self.use_websocket or not existing.get(FUTURES_DATA):
                    tasks["futures"] = _request(
                        self.session, f"{FUTURES_API_URL}/fapi/v1/ticker/24hr"
                    )
                if not self.use_websocket or not existing.get(SPOT_DATA):
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

                failed = self._failed_sections

                futures_data = existing.get(FUTURES_DATA, {})
                if "futures" in fetched and not isinstance(
                    fetched["futures"], Exception
                ):
                    futures_data = _parse_section(
                        failed, self.name, "futures ticker",
                        _index_by_symbol, fetched["futures"], futures_data,
                    )

                spot_data = existing.get(SPOT_DATA, {})
                if "spot" in fetched and not isinstance(
                    fetched["spot"], Exception
                ):
                    spot_data = _parse_section(
                        failed, self.name, "spot ticker",
                        _index_by_symbol, fetched["spot"], spot_data,
                    )

                btcusdt = existing.get(BTCUSDT_PRICE)
                if not isinstance(fetched["btcusdt"], Exception):
                    btcusdt = _parse_section(
                        failed, self.name, "BTCUSDT price",
                        _parse_price, fetched["btcusdt"], btcusdt,
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
# Shared Market Coordinator (% change references + funding)
# ======================================================================


def _refs_from_klines(klines: list, windows: dict[str, int]) -> dict[str, float]:
    """Reference price per window: the close N candles before the current one.

    The current candle is still forming, so with the 60 s refresh the
    measured window averages N candles. Windows the history cannot cover
    yet (fresh listings) are left out.
    """
    closes = [float(k[4]) for k in klines]
    return {
        name: closes[-(n + 1)]
        for name, n in windows.items()
        if len(closes) > n and closes[-(n + 1)] > 0
    }


def _note_fetch(failed: set[str], source: str, what: str, raw) -> bool:
    """True if *raw* is a payload; logs a failed fetch once per streak."""
    tag = f"{what} fetch"
    if isinstance(raw, BinanceRateLimited):
        return False
    if isinstance(raw, Exception):
        if tag not in failed:
            failed.add(tag)
            _LOGGER.warning("Fetching %s failed for %s: %s", what, source, raw)
        return False
    if tag in failed:
        failed.discard(tag)
        _LOGGER.info("Fetching %s recovered for %s", what, source)
    return True


def _parse_funding_info(raw: list) -> dict[str, int]:
    """fundingInfo rows → funding interval hours for symbols that differ."""
    return {row["symbol"]: int(row["fundingIntervalHours"]) for row in raw}


def _parse_funding(
    raw: list, wanted: set[str], intervals: dict[str, int]
) -> dict[str, dict]:
    """premiumIndex rows → funding data for the wanted futures symbols."""
    funding: dict[str, dict] = {}
    for row in raw:
        symbol = row["symbol"]
        if symbol not in wanted:
            continue
        rate = _to_float(row.get("lastFundingRate"), None)
        if rate is None:
            continue
        funding[symbol] = {
            "rate": rate,
            "interval_hours": intervals.get(symbol, DEFAULT_FUNDING_INTERVAL_HOURS),
            "next_funding_time": int(_to_float(row.get("nextFundingTime"))) or None,
            "mark_price": _to_float(row.get("markPrice"), None),
            "index_price": _to_float(row.get("indexPrice"), None),
        }
    return funding


class BinanceMarketCoordinator(DataUpdateCoordinator):
    """Kline reference prices for % changes and futures funding data.

    Shared by all accounts like the price coordinator. Requests only what
    the registered entries enabled, and honours Binance's Retry-After on
    429 / 418 instead of polling through it into an IP ban.
    """

    def __init__(
        self, hass: HomeAssistant, session: aiohttp.ClientSession, shared: dict
    ) -> None:
        self.session = session
        self._shared = shared
        self._failed_sections: set[str] = set()
        self._backoff_until: float = 0
        self._minute_refs: dict[tuple[str, str], dict[str, float]] = {}
        self._hour_refs: dict[tuple[str, str], dict[str, float]] = {}
        self._hour_fetched_at: dict[tuple[str, str], float] = {}
        self._funding_intervals: dict[str, int] = {}
        self._funding_info_at: float = 0

        super().__init__(
            hass,
            _LOGGER,
            config_entry=None,
            name=f"{DOMAIN}_market",
            update_interval=timedelta(seconds=MARKET_UPDATE_INTERVAL),
        )

    def _wanted(self) -> tuple[set[tuple[str, str]], set[str]]:
        """(market, symbol) pairs needing % changes; futures needing funding."""
        changes: set[tuple[str, str]] = set()
        funding: set[str] = set()
        for reg in self._shared["pair_registry"].values():
            if reg.get("changes"):
                changes.update(("futures", s) for s in reg.get("futures", []))
                changes.update(("spot", s) for s in reg.get("spot", []))
            if reg.get("funding"):
                funding.update(reg.get("futures", []))
        return changes, funding

    async def _klines(self, market: str, symbol: str, interval: str, limit: int):
        url = (
            f"{FUTURES_API_URL}/fapi/v1/klines"
            if market == "futures"
            else f"{SPOT_API_URL}/api/v3/klines"
        )
        return await _request(
            self.session,
            url,
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )

    async def _async_update_data(self) -> dict:
        existing = self.data or {CHANGE_REFS: {}, FUNDING_DATA: {}}
        now = time.monotonic()
        if now < self._backoff_until:
            return existing

        changes, funding = self._wanted()
        jobs: list[tuple[str, str, str, int]] = []
        for market, symbol in changes:
            jobs.append((market, symbol, "1m", max(MINUTE_WINDOWS.values()) + 1))
            if (
                now - self._hour_fetched_at.get((market, symbol), 0)
                >= HOURLY_KLINES_REFRESH
            ):
                jobs.append((market, symbol, "1h", max(HOUR_WINDOWS.values()) + 1))
        fetch_info = bool(funding) and (
            now - self._funding_info_at >= FUNDING_INFO_REFRESH
        )

        semaphore = asyncio.Semaphore(KLINE_CONCURRENCY)

        async def kline_job(market, symbol, interval, limit):
            async with semaphore:
                return await self._klines(market, symbol, interval, limit)

        tasks = [kline_job(*job) for job in jobs]
        if fetch_info:
            tasks.append(
                _request(self.session, f"{FUTURES_API_URL}/fapi/v1/fundingInfo")
            )
        if funding:
            tasks.append(
                _request(self.session, f"{FUTURES_API_URL}/fapi/v1/premiumIndex")
            )

        try:
            async with asyncio.timeout(60):
                results = await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError as err:
            raise UpdateFailed("Market data request timed out") from err

        rate_limited = [r for r in results if isinstance(r, BinanceRateLimited)]
        if rate_limited:
            wait = max(r.retry_after for r in rate_limited)
            self._backoff_until = now + wait
            _LOGGER.warning(
                "Binance rate limit hit fetching market data, pausing %d s", wait
            )

        failed = self._failed_sections
        kline_results = results[: len(jobs)]
        for (market, symbol, interval, _), raw in zip(jobs, kline_results):
            if not _note_fetch(
                failed, self.name, f"{market} {symbol} {interval} klines", raw
            ):
                continue
            key = (market, symbol)
            if interval == "1m":
                self._minute_refs[key] = _parse_section(
                    failed, self.name, f"{symbol} 1m klines",
                    partial(_refs_from_klines, windows=MINUTE_WINDOWS),
                    raw, self._minute_refs.get(key, {}),
                )
            else:
                self._hour_refs[key] = _parse_section(
                    failed, self.name, f"{symbol} 1h klines",
                    partial(_refs_from_klines, windows=HOUR_WINDOWS),
                    raw, self._hour_refs.get(key, {}),
                )
                self._hour_fetched_at[key] = now

        # Forget pairs no entry tracks any more.
        for cache in (self._minute_refs, self._hour_refs, self._hour_fetched_at):
            for key in list(cache):
                if key not in changes:
                    del cache[key]

        refs: dict[str, dict[str, dict[str, float]]] = {"futures": {}, "spot": {}}
        for market, symbol in changes:
            merged = {
                **self._minute_refs.get((market, symbol), {}),
                **self._hour_refs.get((market, symbol), {}),
            }
            if merged:
                refs[market][symbol] = merged

        extra = iter(results[len(jobs):])
        if fetch_info:
            info_raw = next(extra)
            if _note_fetch(failed, self.name, "funding info", info_raw):
                self._funding_intervals = _parse_section(
                    failed, self.name, "funding info",
                    _parse_funding_info, info_raw, self._funding_intervals,
                )
                self._funding_info_at = now

        previous_funding = {
            s: v for s, v in existing.get(FUNDING_DATA, {}).items() if s in funding
        }
        funding_data = previous_funding
        if funding:
            premium_raw = next(extra)
            if _note_fetch(failed, self.name, "funding rate", premium_raw):
                funding_data = _parse_section(
                    failed, self.name, "funding rate",
                    partial(
                        _parse_funding, wanted=funding,
                        intervals=self._funding_intervals,
                    ),
                    premium_raw, previous_funding,
                )

        return {CHANGE_REFS: refs, FUNDING_DATA: funding_data}


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
        account_name: str,
        config_entry: ConfigEntry,
    ) -> None:
        self.session = session
        self.api_key = api_key
        self.api_secret = api_secret
        self._backoff_until: float = 0
        self._failed_sections: set[str] = set()

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            # Named per account so log lines say which account failed.
            name=f"{DOMAIN}_account ({account_name})",
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
                    _LOGGER.warning(
                        "Wallet fetch failed for %s: %s", self.name, wallet_raw
                    )
                else:
                    wallet_data = _parse_section(
                        failed, self.name, "wallet balance",
                        partial(
                            _parse_wallets, previous=wallet_data, failed=failed,
                            source=self.name, section="wallet balance",
                        ),
                        wallet_raw, wallet_data,
                    )

                # Wallet (USDT valuation)
                wallet_usd_data = existing.get(WALLET_USD_DATA, {})
                if isinstance(wallet_usd_raw, Exception):
                    _LOGGER.warning(
                        "Wallet USD fetch failed for %s: %s",
                        self.name, wallet_usd_raw,
                    )
                else:
                    wallet_usd_data = _parse_section(
                        failed, self.name, "wallet USD balance",
                        partial(
                            _parse_wallets, previous=wallet_usd_data,
                            failed=failed, source=self.name,
                            section="wallet USD balance",
                        ),
                        wallet_usd_raw, wallet_usd_data,
                    )

                # PnL — keep only open positions
                pnl_data = existing.get(PNL_DATA, [])
                if isinstance(pnl_raw, Exception):
                    _LOGGER.warning(
                        "PnL fetch failed for %s: %s", self.name, pnl_raw
                    )
                else:
                    pnl_data = _parse_section(
                        failed, self.name, "futures position",
                        _parse_positions, pnl_raw, pnl_data,
                    )

                # Margin — account-level + per-position margin ratios
                margin_data = existing.get(MARGIN_DATA, {})
                if isinstance(account_raw, Exception):
                    _LOGGER.warning(
                        "Futures account fetch failed for %s: %s",
                        self.name, account_raw,
                    )
                else:
                    margin_data = _parse_section(
                        failed, self.name, "futures account",
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
    created = shared is None

    if created:
        # First entry — bootstrap shared layer.
        shared = {
            "price_coordinator": BinancePriceCoordinator(
                hass, session, interval, use_ws,
            ),
            "ws_manager": None,
            "pair_registry": {},
            "use_websocket": use_ws,
        }
        shared["market_coordinator"] = BinanceMarketCoordinator(
            hass, session, shared
        )
        domain_data[SHARED_KEY] = shared

    # Register before awaiting anything: entries set up concurrently, and a
    # failing one tears the shared layer down if the registry looks empty.
    shared["pair_registry"][entry.entry_id] = {
        "futures": futures_pairs,
        "spot": spot_pairs,
        "changes": entry.options.get(CONF_PRICE_CHANGES, DEFAULT_PRICE_CHANGES),
        "funding": entry.options.get(
            CONF_FUNDING_SENSORS, DEFAULT_FUNDING_SENSORS
        ),
    }

    coordinator = shared["price_coordinator"]
    market = shared["market_coordinator"]
    if created:
        await coordinator.async_register_shutdown()
        await market.async_register_shutdown()

    # Also covers a bootstrap that failed while another entry kept the
    # shared layer alive.
    if coordinator.data is None:
        await coordinator.async_refresh()
        if coordinator.data is None:
            raise ConfigEntryNotReady("Binance price data not available yet")

    # Restart WebSocket with merged pairs.
    await _refresh_websocket(hass)

    # Fetch klines / funding for the new pair set without blocking setup;
    # the debouncer folds several entries starting together into one run.
    hass.async_create_background_task(
        market.async_request_refresh(), f"{DOMAIN} market data refresh"
    )

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
        hass.data[DOMAIN].pop(SHARED_KEY, None)
        ws: BinanceWebSocketManager | None = shared.get("ws_manager")
        if ws:
            await ws.stop()
        await shared["price_coordinator"].async_shutdown()
        await shared["market_coordinator"].async_shutdown()
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

    try:
        # --- Shared price coordinator ---
        await _ensure_shared(hass, entry, futures_pairs, spot_pairs)

        # --- Per-account coordinator ---
        account_coordinator = BinanceAccountCoordinator(
            hass,
            session,
            api_key=entry.data[CONF_API_KEY],
            api_secret=entry.data[CONF_API_SECRET],
            update_interval=interval,
            account_name=entry.data.get(CONF_ACCOUNT_NAME, "Account"),
            config_entry=entry,
        )
        await account_coordinator.async_config_entry_first_refresh()
    except Exception:
        # HA skips async_unload_entry for an entry left in setup retry, so
        # drop this entry's pairs from the shared layer here.
        await _unregister_shared(hass, entry.entry_id)
        raise

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
