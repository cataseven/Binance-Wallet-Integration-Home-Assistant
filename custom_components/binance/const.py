"""Constants for the Binance integration."""

from dataclasses import dataclass

DOMAIN = "binance"
PLATFORMS = ["sensor"]

# --- Configuration Constants ---
CONF_API_KEY = "api_key"
CONF_API_SECRET = "api_secret"
CONF_ACCOUNT_NAME = "account_name"
CONF_FUTURES_PAIRS = "futures_pairs"
CONF_SPOT_PAIRS = "spot_pairs"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_USE_WEBSOCKET = "use_websocket"
CONF_PRICE_CHANGES = "price_changes"
CONF_FUNDING_SENSORS = "funding_sensors"
CONF_POSITION_SENSORS = "position_sensors"

# --- Defaults ---
DEFAULT_UPDATE_INTERVAL = 60  # seconds
DEFAULT_USE_WEBSOCKET = True
DEFAULT_PRICE_CHANGES = True
DEFAULT_FUNDING_SENSORS = True
DEFAULT_POSITION_SENSORS = True

# --- Data Keys (shared price coordinator) ---
FUTURES_DATA = "futures_data"
SPOT_DATA = "spot_data"
BTCUSDT_PRICE = "btcusdt_price"

# --- Data Keys (shared market coordinator) ---
CHANGE_REFS = "change_refs"
FUNDING_DATA = "funding_data"

# --- Market coordinator (klines + funding) ---
MARKET_UPDATE_INTERVAL = 60  # seconds; 1m reference prices
HOURLY_KLINES_REFRESH = 1800  # seconds; 1h candles for the 1w / 1M windows
FUNDING_INFO_REFRESH = 3600  # seconds; per-symbol funding intervals
DEFAULT_FUNDING_INTERVAL_HOURS = 8
KLINE_CONCURRENCY = 8
# Window name -> candles back. Reference = close N candles before the
# current (in-progress) one, so each window needs N + 1 candles.
MINUTE_WINDOWS = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "2h": 120, "4h": 240}
HOUR_WINDOWS = {"1w": 168, "1M": 720}
CHANGE_WINDOWS = ("1m", "5m", "15m", "1h", "2h", "4h", "1d", "1w", "1M")

# --- Data Keys (per-account coordinator) ---
WALLET_DATA = "wallet_data"
WALLET_USD_DATA = "wallet_usd_data"
PNL_DATA = "pnl_data"
MARGIN_DATA = "margin_data"

# --- hass.data layout keys ---
SHARED_KEY = "_shared"

# --- REST API Endpoints ---
FUTURES_API_URL = "https://fapi.binance.com"
SPOT_API_URL = "https://api.binance.com"

# --- WebSocket Endpoints ---
SPOT_WS_URL = "wss://stream.binance.com:9443/stream"
# Binance migrated USDⓈ-M Futures WebSocket routing on 2026-04-23.
# @ticker / @markPrice / @kline streams now live under "/market".
# Spot WebSocket (above) was NOT affected by this change.
# See: https://developers.binance.com/docs/derivatives/usds-margined-futures/
#      websocket-market-streams/Important-WebSocket-Change-Notice
FUTURES_WS_URL = "wss://fstream.binance.com/market/stream"

# --- WebSocket ---
WS_MAX_STREAMS_PER_CONNECTION = 200
WS_RECONNECT_DELAY = 5  # seconds

# --- Rate Limit ---
RATE_LIMIT_BACKOFF_BASE = 5  # seconds
RATE_LIMIT_BACKOFF_MAX = 300  # 5 minutes max backoff


@dataclass(frozen=True)
class QuoteAssetInfo:
    """Display unit and icon for a quote asset."""

    unit: str
    icon: str


# Single source of truth: quote asset → (display unit, icon).
QUOTE_ASSET_CONFIG: dict[str, QuoteAssetInfo] = {
    # USD-pegged stablecoins
    "FDUSD": QuoteAssetInfo("USD", "mdi:currency-usd"),
    "USDT": QuoteAssetInfo("USD", "mdi:currency-usd"),
    "USDC": QuoteAssetInfo("USD", "mdi:currency-usd"),
    "TUSD": QuoteAssetInfo("USD", "mdi:currency-usd"),
    "BUSD": QuoteAssetInfo("USD", "mdi:currency-usd"),
    "DUSD": QuoteAssetInfo("USD", "mdi:currency-usd"),
    "DAI": QuoteAssetInfo("USD", "mdi:currency-usd"),
    # EUR-pegged
    "AEUR": QuoteAssetInfo("EUR", "mdi:currency-eur"),
    "EURI": QuoteAssetInfo("EUR", "mdi:currency-eur"),
    "EUR": QuoteAssetInfo("EUR", "mdi:currency-eur"),
    # Fiat currencies
    "TRY": QuoteAssetInfo("TRY", "mdi:currency-try"),
    "BRL": QuoteAssetInfo("BRL", "mdi:currency-brl"),
    "UAH": QuoteAssetInfo("UAH", "mdi:currency-uah"),
    "GBP": QuoteAssetInfo("GBP", "mdi:currency-gbp"),
    "JPY": QuoteAssetInfo("JPY", "mdi:currency-jpy"),
    "RUB": QuoteAssetInfo("RUB", "mdi:currency-rub"),
    "PLN": QuoteAssetInfo("PLN", "mdi:cash"),
    "RON": QuoteAssetInfo("RON", "mdi:cash"),
    "ARS": QuoteAssetInfo("ARS", "mdi:cash"),
    "MXN": QuoteAssetInfo("MXN", "mdi:cash"),
    "COP": QuoteAssetInfo("COP", "mdi:cash"),
    "CZK": QuoteAssetInfo("CZK", "mdi:cash"),
    "ZAR": QuoteAssetInfo("ZAR", "mdi:cash"),
    "AUD": QuoteAssetInfo("AUD", "mdi:cash"),
    "IDR": QuoteAssetInfo("IDR", "mdi:cash"),
    "NGN": QuoteAssetInfo("NGN", "mdi:cash"),
    # Crypto
    "BTC": QuoteAssetInfo("BTC", "mdi:bitcoin"),
    "ETH": QuoteAssetInfo("ETH", "mdi:ethereum"),
    "BNB": QuoteAssetInfo("BNB", "mdi:cash"),
    "DOGE": QuoteAssetInfo("DOGE", "mdi:dog"),
}

# Pre-sorted keys: longest suffix first for unambiguous matching.
QUOTE_ASSET_KEYS_SORTED: list[str] = sorted(
    QUOTE_ASSET_CONFIG.keys(), key=len, reverse=True
)

# Fiat units eligible for SensorDeviceClass.MONETARY.
FIAT_UNITS: set[str] = {
    "USD", "EUR", "TRY", "BRL", "UAH", "GBP", "JPY", "RUB",
    "PLN", "RON", "ARS", "MXN", "COP", "CZK", "ZAR", "AUD",
    "IDR", "NGN",
}
