"""Binance sensor entities."""

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BTCUSDT_PRICE,
    CONF_ACCOUNT_NAME,
    CONF_FUTURES_PAIRS,
    CONF_SPOT_PAIRS,
    DOMAIN,
    FUTURES_DATA,
    PNL_DATA,
    QUOTE_ASSET_CONFIG,
    QUOTE_ASSET_KEYS_SORTED,
    SHARED_KEY,
    SPOT_DATA,
    WALLET_DATA,
)

_LOGGER = logging.getLogger(__name__)


def _resolve_quote_asset(symbol: str) -> str | None:
    """Return the quote asset suffix for *symbol*, or None if unknown."""
    for asset in QUOTE_ASSET_KEYS_SORTED:
        if symbol.endswith(asset) and len(symbol) > len(asset):
            return asset
    return None


def _all_desired_price_uids(hass: HomeAssistant) -> set[str]:
    """Collect desired price sensor unique IDs across ALL config entries."""
    uids: set[str] = set()
    shared = hass.data.get(DOMAIN, {}).get(SHARED_KEY)
    if not shared:
        return uids
    for pairs in shared["pair_registry"].values():
        for pair in pairs.get("futures", []):
            uids.add(f"binance_futures_{pair}")
        for pair in pairs.get("spot", []):
            uids.add(f"binance_spot_{pair}")
    return uids


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Binance sensors from a config entry."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    account_coordinator = entry_data["account_coordinator"]

    shared = hass.data[DOMAIN][SHARED_KEY]
    price_coordinator = shared["price_coordinator"]

    entity_registry = async_get_entity_registry(hass)
    account_name = config_entry.data.get(CONF_ACCOUNT_NAME, "Account")
    entry_id = config_entry.entry_id

    futures_pairs = config_entry.options.get(
        CONF_FUTURES_PAIRS, config_entry.data.get(CONF_FUTURES_PAIRS, [])
    )
    spot_pairs = config_entry.options.get(
        CONF_SPOT_PAIRS, config_entry.data.get(CONF_SPOT_PAIRS, [])
    )

    # --- Build desired unique IDs for THIS entry's own entities ---
    desired_own_uids: set[str] = set()

    fmt_account = account_name.lower().replace(" ", "_")
    wallet_data = (account_coordinator.data or {}).get(WALLET_DATA, {})
    for wallet_name in wallet_data:
        fmt_name = wallet_name.lower().replace(" ", "_")
        desired_own_uids.add(f"binance_wallet_{fmt_account}_{fmt_name}_btc")
        desired_own_uids.add(f"binance_wallet_{fmt_account}_{fmt_name}_usdt")
    desired_own_uids.add(f"binance_pnl_{fmt_account}_total")

    # Price sensors this entry claims.
    for pair in futures_pairs:
        desired_own_uids.add(f"binance_futures_{pair}")
    for pair in spot_pairs:
        desired_own_uids.add(f"binance_spot_{pair}")

    # Union of ALL entries' price UIDs (so we don't delete a sensor
    # that another entry still needs).
    all_price_uids = _all_desired_price_uids(hass)

    # --- Remove stale entities for THIS config entry ---
    for entity in list(entity_registry.entities.values()):
        if entity.config_entry_id != config_entry.entry_id:
            continue
        if entity.unique_id in desired_own_uids:
            continue
        # Don't remove if another entry still wants this price sensor.
        if entity.unique_id in all_price_uids:
            continue
        _LOGGER.debug(
            "Removing stale sensor: %s (%s)", entity.entity_id, entity.unique_id
        )
        entity_registry.async_remove(entity.entity_id)

    # --- Create sensors ---
    sensors: list[SensorEntity] = []

    # Price sensors — create if:
    #   1. Not registered at all (new sensor), OR
    #   2. Already registered under THIS entry (restore on HA restart)
    # Skip only if registered under a DIFFERENT entry (avoid duplicates).
    for pair in futures_pairs:
        uid = f"binance_futures_{pair}"
        existing_eid = entity_registry.async_get_entity_id("sensor", DOMAIN, uid)
        if existing_eid is None:
            sensors.append(
                BinancePriceSensor(price_coordinator, pair, "futures")
            )
        else:
            entity_entry = entity_registry.async_get(existing_eid)
            if entity_entry and entity_entry.config_entry_id == config_entry.entry_id:
                sensors.append(
                    BinancePriceSensor(price_coordinator, pair, "futures")
                )

    for pair in spot_pairs:
        uid = f"binance_spot_{pair}"
        existing_eid = entity_registry.async_get_entity_id("sensor", DOMAIN, uid)
        if existing_eid is None:
            sensors.append(
                BinancePriceSensor(price_coordinator, pair, "spot")
            )
        else:
            entity_entry = entity_registry.async_get(existing_eid)
            if entity_entry and entity_entry.config_entry_id == config_entry.entry_id:
                sensors.append(
                    BinancePriceSensor(price_coordinator, pair, "spot")
                )

    # Wallet sensors — per-account.
    for wallet_name in wallet_data:
        sensors.append(
            BinanceWalletSensor(
                account_coordinator, price_coordinator,
                wallet_name, account_name, entry_id, "btc",
            )
        )
        sensors.append(
            BinanceWalletSensor(
                account_coordinator, price_coordinator,
                wallet_name, account_name, entry_id, "usdt",
            )
        )

    # PnL sensor — per-account.
    sensors.append(BinancePnlSensor(account_coordinator, account_name, entry_id))

    async_add_entities(sensors)


# ======================================================================
# Price Sensor (uses shared price coordinator)
# ======================================================================


class BinancePriceSensor(CoordinatorEntity, SensorEntity):
    """Binance trading pair price sensor."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, symbol: str, market_type: str) -> None:
        super().__init__(coordinator)
        self._symbol = symbol
        self._market_type = market_type
        self._data_key = FUTURES_DATA if market_type == "futures" else SPOT_DATA

        self._attr_name = f"Binance {market_type.capitalize()} {symbol} Price"
        self._attr_unique_id = f"binance_{market_type}_{symbol}"

        quote = _resolve_quote_asset(symbol)
        if quote and quote in QUOTE_ASSET_CONFIG:
            info = QUOTE_ASSET_CONFIG[quote]
            self._attr_native_unit_of_measurement = info.unit
            self._attr_icon = info.icon
        else:
            self._attr_icon = "mdi:cash"

    @property
    def _symbol_data(self) -> dict | None:
        data = self.coordinator.data
        if data and self._data_key in data:
            return data[self._data_key].get(self._symbol)
        return None

    @property
    def available(self) -> bool:
        return super().available and self._symbol_data is not None

    @property
    def native_value(self):
        sym = self._symbol_data
        if sym:
            return float(sym.get("lastPrice", 0))
        return None

    @property
    def extra_state_attributes(self) -> dict:
        sym = self._symbol_data
        if not sym:
            return {}
        return {
            "price_change_percent": float(sym.get("priceChangePercent", 0)),
            "high_price": float(sym.get("highPrice", 0)),
            "low_price": float(sym.get("lowPrice", 0)),
            "volume": float(sym.get("volume", 0)),
            "quote_volume": float(sym.get("quoteVolume", 0)),
        }

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f"binance_{self._market_type}_market")},
            "name": f"Binance {self._market_type.capitalize()} Market",
            "manufacturer": "Binance",
            "model": "Price Tickers",
        }


# ======================================================================
# Unified Wallet Sensor
# ======================================================================


class BinanceWalletSensor(CoordinatorEntity, SensorEntity):
    """Binance wallet balance sensor (BTC or USDT equivalent)."""

    def __init__(
        self,
        account_coordinator,
        price_coordinator,
        wallet_name: str,
        account_name: str,
        entry_id: str,
        currency: str,
    ) -> None:
        # CoordinatorEntity tracks the account coordinator for availability.
        super().__init__(account_coordinator)
        self._price_coordinator = price_coordinator
        self._wallet_name = wallet_name
        self._currency = currency
        self._entry_id = entry_id
        self._account_name = account_name

        fmt_account = account_name.lower().replace(" ", "_")
        fmt_name = wallet_name.lower().replace(" ", "_")

        self._attr_unique_id = (
            f"binance_wallet_{fmt_account}_{fmt_name}_{currency}"
        )
        self._attr_name = (
            f"Binance {account_name} {wallet_name} Wallet {currency.upper()}"
        )

        if currency == "btc":
            self._attr_icon = "mdi:bitcoin"
            self._attr_native_unit_of_measurement = "BTC"
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            self._attr_icon = "mdi:currency-usd"
            self._attr_native_unit_of_measurement = "USD"
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_state_class = SensorStateClass.TOTAL

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        data = self.coordinator.data
        if not data or self._wallet_name not in data.get(WALLET_DATA, {}):
            return False
        if self._currency == "usdt":
            price_data = self._price_coordinator.data
            if not price_data or price_data.get(BTCUSDT_PRICE) is None:
                return False
        return True

    @property
    def native_value(self):
        data = self.coordinator.data
        if not data:
            return None

        btc_balance = data.get(WALLET_DATA, {}).get(self._wallet_name)
        if btc_balance is None:
            return None

        if self._currency == "usdt":
            price_data = self._price_coordinator.data
            if not price_data:
                return None
            price = price_data.get(BTCUSDT_PRICE)
            if price is None:
                return None
            return round(btc_balance * price, 2)

        return btc_balance

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f"binance_account_{self._entry_id}")},
            "name": f"Binance {self._account_name}",
            "manufacturer": "Binance",
            "model": "Wallets",
        }


# ======================================================================
# Futures PnL Sensor
# ======================================================================


class BinancePnlSensor(CoordinatorEntity, SensorEntity):
    """Total unrealized PnL across all open futures positions."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "USD"
    _attr_icon = "mdi:chart-line"

    def __init__(self, coordinator, account_name: str, entry_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._account_name = account_name
        fmt_account = account_name.lower().replace(" ", "_")

        self._attr_unique_id = f"binance_pnl_{fmt_account}_total"
        self._attr_name = f"Binance {account_name} Futures PnL"

    @property
    def _positions(self) -> list[dict]:
        data = self.coordinator.data
        if data:
            return data.get(PNL_DATA, [])
        return []

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self):
        positions = self._positions
        if not positions:
            return 0.0
        return round(sum(p["unRealizedProfit"] for p in positions), 2)

    @property
    def extra_state_attributes(self) -> dict:
        positions = self._positions
        if not positions:
            return {"open_positions": 0}

        attrs = {"open_positions": len(positions)}
        for pos in positions:
            prefix = pos["symbol"]
            side = pos.get("positionSide", "BOTH")
            if side != "BOTH":
                prefix = f"{prefix}_{side}"
            attrs[f"{prefix}_amount"] = pos["positionAmt"]
            attrs[f"{prefix}_entry_price"] = pos["entryPrice"]
            attrs[f"{prefix}_mark_price"] = pos["markPrice"]
            attrs[f"{prefix}_pnl"] = pos["unRealizedProfit"]
            attrs[f"{prefix}_leverage"] = pos["leverage"]
            attrs[f"{prefix}_margin_type"] = pos["marginType"]
            attrs[f"{prefix}_liquidation_price"] = pos["liquidationPrice"]
        return attrs

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f"binance_account_{self._entry_id}")},
            "name": f"Binance {self._account_name}",
            "manufacturer": "Binance",
            "model": "Wallets",
        }
