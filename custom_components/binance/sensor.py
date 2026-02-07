"""Support for Binance sensors."""
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry

from .const import (
    DOMAIN,
    CONF_ACCOUNT_NAME,
    CONF_FUTURES_PAIRS,
    CONF_SPOT_PAIRS,
    FUTURES_DATA,
    SPOT_DATA,
    WALLET_DATA,
    BTCUSDT_PRICE,
)

_LOGGER = logging.getLogger(__name__)

# --- Mappings for Units and Icons based on user request ---
UNIT_MAP = {
    "USDT": "USD", "DUSD": "USD", "USDC": "USD", "TUSD": "USD", "BUSD": "USD", "DAI": "USD",
    "AEUR": "EUR", "EURI": "EUR",
    "TRY": "TRY",
    "BTC": "BTC",
    "DOGE": "DOGE",
    "ETH": "ETH",
    "BNB": "BNB",
    "BRL": "BRL",
    "UAH": "UAH",
    "ZAR": "ZAR",
    "PLN": "PLN",
    "RON": "RON",
    "ARS": "ARS",
    "JPY": "JPY",
    "MXN": "MXN",
    "COP": "COP",
    "CZK": "CZK",
}

ICON_MAP = {
    "USDT": "mdi:currency-usd", "DUSD": "mdi:currency-usd", "USDC": "mdi:currency-usd", "TUSD": "mdi:currency-usd", "BUSD": "mdi:currency-usd", "DAI": "mdi:currency-usd",
    "AEUR": "mdi:currency-eur", "EURI": "mdi:currency-eur",
    "TRY": "mdi:currency-try",
    "BTC": "mdi:bitcoin",
    "ETH": "mdi:ethereum",
    "DOGE": "mdi:dog",
    "BRL": "mdi:currency-brl",
    "UAH": "mdi:currency-uah",
    "PLN": "mdi:cash",
    "JPY": "mdi:currency-jpy",
    "MXN": "mdi:cash",
    "ZAR": "mdi:cash",
    "RON": "mdi:cash",
    "ARS": "mdi:cash",
    "COP": "mdi:cash",
    "CZK": "mdi:cash",
}


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up Binance sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    entity_registry = async_get_entity_registry(hass)

    account_name = config_entry.data.get(CONF_ACCOUNT_NAME, "Account")
    entry_id = config_entry.entry_id

    futures_pairs = config_entry.options.get(
        CONF_FUTURES_PAIRS, config_entry.data.get(CONF_FUTURES_PAIRS, [])
    )
    spot_pairs = config_entry.options.get(
        CONF_SPOT_PAIRS, config_entry.data.get(CONF_SPOT_PAIRS, [])
    )

    # --- Build desired unique_ids for stale entity cleanup ---
    desired_unique_ids = set()

    # Price sensors use a global unique_id (shared across entries)
    for pair in futures_pairs:
        desired_unique_ids.add(f"binance_futures_{pair}")
    for pair in spot_pairs:
        desired_unique_ids.add(f"binance_spot_{pair}")

    # Wallet sensors are per-account (use entry_id)
    formatted_account = account_name.lower().replace(" ", "_")
    if coordinator.data and coordinator.data.get(WALLET_DATA):
        for wallet_name in coordinator.data[WALLET_DATA]:
            formatted_name = wallet_name.lower().replace(" ", "_")
            desired_unique_ids.add(
                f"binance_wallet_{formatted_account}_{formatted_name}_btc"
            )
            desired_unique_ids.add(
                f"binance_wallet_{formatted_account}_{formatted_name}_usdt"
            )

    # --- Remove stale entities belonging to THIS entry ---
    registered_entities = [
        entry
        for entry in entity_registry.entities.values()
        if entry.config_entry_id == config_entry.entry_id
    ]

    for entity in registered_entities:
        if entity.unique_id not in desired_unique_ids:
            _LOGGER.debug(
                "Removing stale sensor: %s (%s)", entity.entity_id, entity.unique_id
            )
            entity_registry.async_remove(entity.entity_id)

    # --- Create sensors ---
    sensors_to_add = []

    # Price sensors: only add if no other entry already registered this unique_id
    for pair in futures_pairs:
        uid = f"binance_futures_{pair}"
        existing = entity_registry.async_get_entity_id("sensor", DOMAIN, uid)
        if existing is None:
            sensors_to_add.append(BinancePriceSensor(coordinator, pair, "futures"))

    for pair in spot_pairs:
        uid = f"binance_spot_{pair}"
        existing = entity_registry.async_get_entity_id("sensor", DOMAIN, uid)
        if existing is None:
            sensors_to_add.append(BinancePriceSensor(coordinator, pair, "spot"))

    # Wallet sensors: always per-account
    if coordinator.data and coordinator.data.get(WALLET_DATA):
        for wallet_name in coordinator.data[WALLET_DATA].keys():
            sensors_to_add.append(
                BinanceWalletBtcSensor(
                    coordinator, wallet_name, account_name, entry_id
                )
            )
            sensors_to_add.append(
                BinanceWalletUsdtSensor(
                    coordinator, wallet_name, account_name, entry_id
                )
            )

    async_add_entities(sensors_to_add)


class BinancePriceSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Binance Price Sensor."""

    def __init__(self, coordinator, symbol, market_type):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._symbol = symbol
        self._market_type = market_type
        self._attr_name = f"Binance {market_type.capitalize()} {self._symbol} Price"
        self._attr_unique_id = f"binance_{self._market_type}_{self._symbol}"

        # Determine quote asset for dynamic icon and unit
        self._quote_asset = None
        for asset in sorted(UNIT_MAP.keys(), key=len, reverse=True):
            if self._symbol.endswith(asset):
                self._quote_asset = asset
                break

        self._attr_icon = ICON_MAP.get(self._quote_asset, "mdi:cash")
        self._attr_native_unit_of_measurement = UNIT_MAP.get(self._quote_asset)

    @property
    def data_key(self):
        return FUTURES_DATA if self._market_type == "futures" else SPOT_DATA

    @property
    def symbol_data(self):
        if self.coordinator.data and self.data_key in self.coordinator.data:
            return self.coordinator.data[self.data_key].get(self._symbol)
        return None

    @property
    def available(self) -> bool:
        return super().available and self.symbol_data is not None

    @property
    def native_value(self):
        if self.symbol_data:
            return float(self.symbol_data.get("lastPrice"))
        return None

    @property
    def extra_state_attributes(self):
        if self.symbol_data:
            return {
                "price_change_percent": float(
                    self.symbol_data.get("priceChangePercent", 0)
                ),
                "high_price": float(self.symbol_data.get("highPrice", 0)),
                "low_price": float(self.symbol_data.get("lowPrice", 0)),
                "volume": float(self.symbol_data.get("volume", 0)),
                "quote_volume": float(self.symbol_data.get("quoteVolume", 0)),
            }
        return {}

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"binance_{self._market_type}_market")},
            "name": f"Binance {self._market_type.capitalize()} Market",
            "manufacturer": "Binance",
            "model": "Price Tickers",
        }


class BinanceWalletBtcSensor(CoordinatorEntity, SensorEntity):
    """Representation of a single Binance Wallet Sensor in BTC."""

    def __init__(self, coordinator, wallet_name, account_name, entry_id):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._wallet_name = wallet_name
        self._account_name = account_name
        self._entry_id = entry_id
        formatted_account = account_name.lower().replace(" ", "_")
        formatted_name = wallet_name.lower().replace(" ", "_")

        self._attr_name = f"Binance {self._account_name} {self._wallet_name} Wallet BTC"
        self._attr_unique_id = (
            f"binance_wallet_{formatted_account}_{formatted_name}_btc"
        )
        self._attr_icon = "mdi:bitcoin"
        self._attr_native_unit_of_measurement = "BTC"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.get(WALLET_DATA) is not None
            and self._wallet_name in self.coordinator.data[WALLET_DATA]
        )

    @property
    def native_value(self):
        if self.available:
            return self.coordinator.data[WALLET_DATA].get(self._wallet_name)
        return None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"binance_account_{self._entry_id}")},
            "name": f"Binance {self._account_name}",
            "manufacturer": "Binance",
            "model": "Wallets",
        }


class BinanceWalletUsdtSensor(CoordinatorEntity, SensorEntity):
    """Representation of a single Binance Wallet Sensor in USDT."""

    def __init__(self, coordinator, wallet_name, account_name, entry_id):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._wallet_name = wallet_name
        self._account_name = account_name
        self._entry_id = entry_id
        formatted_account = account_name.lower().replace(" ", "_")
        formatted_name = wallet_name.lower().replace(" ", "_")

        self._attr_name = (
            f"Binance {self._account_name} {self._wallet_name} Wallet USDT"
        )
        self._attr_unique_id = (
            f"binance_wallet_{formatted_account}_{formatted_name}_usdt"
        )
        self._attr_icon = "mdi:currency-usd"
        self._attr_native_unit_of_measurement = "USDT"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.get(WALLET_DATA) is not None
            and self.coordinator.data.get(BTCUSDT_PRICE) is not None
            and self._wallet_name in self.coordinator.data[WALLET_DATA]
        )

    @property
    def native_value(self):
        """Return the state of the sensor (balance in USDT)."""
        if not self.available:
            return None

        btc_balance = self.coordinator.data[WALLET_DATA].get(self._wallet_name)
        btcusdt_price = self.coordinator.data.get(BTCUSDT_PRICE)

        if btc_balance is None or btcusdt_price is None:
            return None

        return round(btc_balance * btcusdt_price, 2)

    @property
    def device_info(self):
        """Return device information to group wallet sensors under one account device."""
        return {
            "identifiers": {(DOMAIN, f"binance_account_{self._entry_id}")},
            "name": f"Binance {self._account_name}",
            "manufacturer": "Binance",
            "model": "Wallets",
        }
