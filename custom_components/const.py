"""Constants for the Binance integration."""

DOMAIN = "binance"
PLATFORMS = ["sensor"]

# --- Configuration Constants ---
CONF_API_KEY = "api_key"
CONF_API_SECRET = "api_secret"
CONF_FUTURES_PAIRS = "futures_pairs"
CONF_SPOT_PAIRS = "spot_pairs"
CONF_UPDATE_INTERVAL = "update_interval"

# --- Defaults ---
DEFAULT_UPDATE_INTERVAL = 60  # seconds

# --- Coordinator Data Keys ---
FUTURES_DATA = "futures_data"
SPOT_DATA = "spot_data"
WALLET_DATA = "wallet_data"
BTCUSDT_PRICE = "btcusdt_price"

# --- API Endpoints ---
FUTURES_API_URL = "https://fapi.binance.com"
SPOT_API_URL = "https://api.binance.com"
