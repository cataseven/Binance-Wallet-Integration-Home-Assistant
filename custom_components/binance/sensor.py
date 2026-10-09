"""Binance sensor entities."""

from datetime import UTC, datetime
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import _to_float
from .const import (
    BTCUSDT_PRICE,
    CHANGE_REFS,
    CHANGE_WINDOWS,
    CONF_ACCOUNT_NAME,
    CONF_FUNDING_SENSORS,
    CONF_FUTURES_PAIRS,
    CONF_POSITION_SENSORS,
    CONF_SPOT_PAIRS,
    DEFAULT_FUNDING_SENSORS,
    DEFAULT_POSITION_SENSORS,
    DOMAIN,
    FUNDING_DATA,
    FUTURES_DATA,
    MARGIN_DATA,
    PNL_DATA,
    QUOTE_ASSET_CONFIG,
    QUOTE_ASSET_KEYS_SORTED,
    SHARED_KEY,
    SPOT_DATA,
    WALLET_DATA,
    WALLET_USD_DATA,
)

_LOGGER = logging.getLogger(__name__)


def _resolve_quote_asset(symbol: str) -> str | None:
    """Return the quote asset suffix for *symbol*, or None if unknown."""
    for asset in QUOTE_ASSET_KEYS_SORTED:
        if symbol.endswith(asset) and len(symbol) > len(asset):
            return asset
    return None


def _position_prefix(symbol: str, side: str) -> str:
    """Display / unique-id prefix for a position (hedge-mode aware)."""
    return symbol if side == "BOTH" else f"{symbol}_{side}"


def _position_roe(pos: dict) -> float | None:
    """ROE % as the Binance UI shows it: PnL over the initial margin."""
    initial_margin = (
        abs(pos["positionAmt"]) * pos["entryPrice"] / max(pos["leverage"], 1)
    )
    if initial_margin <= 0:
        return None
    return round(pos["unRealizedProfit"] / initial_margin * 100, 2)


def _liquidation_distance(pos: dict) -> float | None:
    """% the mark price can move against the position before liquidation."""
    mark, liquidation = pos["markPrice"], pos["liquidationPrice"]
    if mark <= 0 or liquidation <= 0:
        return None
    move = mark - liquidation if pos["positionAmt"] > 0 else liquidation - mark
    return round(move / mark * 100, 2)


POSITION_SENSOR_KINDS = ("pnl", "roi", "liq_distance")


def _position_uid(kind: str, fmt_account: str, symbol: str, side: str) -> str:
    prefix = _position_prefix(symbol, side).lower()
    return f"binance_position_{kind}_{fmt_account}_{prefix}"


def _all_desired_market_uids(hass: HomeAssistant) -> set[str]:
    """Price and funding sensor unique IDs wanted by ANY config entry."""
    uids: set[str] = set()
    shared = hass.data.get(DOMAIN, {}).get(SHARED_KEY)
    if not shared:
        return uids
    for pairs in shared["pair_registry"].values():
        for pair in pairs.get("futures", []):
            uids.add(f"binance_futures_{pair}")
            if pairs.get("funding"):
                uids.add(f"binance_futures_funding_{pair}")
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
    market_coordinator = shared["market_coordinator"]

    entity_registry = async_get_entity_registry(hass)
    account_name = config_entry.data.get(CONF_ACCOUNT_NAME, "Account")
    entry_id = config_entry.entry_id

    futures_pairs = config_entry.options.get(
        CONF_FUTURES_PAIRS, config_entry.data.get(CONF_FUTURES_PAIRS, [])
    )
    spot_pairs = config_entry.options.get(
        CONF_SPOT_PAIRS, config_entry.data.get(CONF_SPOT_PAIRS, [])
    )
    funding_enabled = config_entry.options.get(
        CONF_FUNDING_SENSORS, DEFAULT_FUNDING_SENSORS
    )
    positions_enabled = config_entry.options.get(
        CONF_POSITION_SENSORS, DEFAULT_POSITION_SENSORS
    )

    # --- Build desired unique IDs for THIS entry's own entities ---
    desired_own_uids: set[str] = set()

    fmt_account = account_name.lower().replace(" ", "_")
    coord_data = account_coordinator.data or {}
    # Union of both valuations' wallet names: one failed fetch during a
    # reload must not orphan the other currency's sensors.
    wallet_names = sorted(
        set(coord_data.get(WALLET_DATA, {}))
        | set(coord_data.get(WALLET_USD_DATA, {}))
    )
    for wallet_name in wallet_names:
        fmt_name = wallet_name.lower().replace(" ", "_")
        desired_own_uids.add(f"binance_wallet_{fmt_account}_{fmt_name}_btc")
        desired_own_uids.add(f"binance_wallet_{fmt_account}_{fmt_name}_usdt")
    desired_own_uids.add(f"binance_pnl_{fmt_account}_total")
    desired_own_uids.add(f"binance_margin_ratio_{fmt_account}")

    # Per-position sensors for positions open right now; closed positions
    # drop out here and their entities are cleaned up below.
    open_positions = [
        (pos["symbol"], pos.get("positionSide", "BOTH"))
        for pos in coord_data.get(PNL_DATA, [])
    ] if positions_enabled else []
    for symbol, side in open_positions:
        for kind in POSITION_SENSOR_KINDS:
            desired_own_uids.add(
                _position_uid(kind, fmt_account, symbol, side)
            )

    # Price / funding sensors this entry claims.
    for pair in futures_pairs:
        desired_own_uids.add(f"binance_futures_{pair}")
        if funding_enabled:
            desired_own_uids.add(f"binance_futures_funding_{pair}")
    for pair in spot_pairs:
        desired_own_uids.add(f"binance_spot_{pair}")

    # Union of ALL entries' market UIDs (so we don't delete a sensor
    # that another entry still needs).
    all_market_uids = _all_desired_market_uids(hass)

    # --- Remove stale entities for THIS config entry ---
    for entity in list(entity_registry.entities.values()):
        if entity.config_entry_id != config_entry.entry_id:
            continue
        if entity.unique_id in desired_own_uids:
            continue
        # Don't remove if another entry still wants this market sensor.
        if entity.unique_id in all_market_uids:
            continue
        # If BOTH wallet fetches failed (transient outage during reload),
        # keep the registered wallet entities instead of wiping them.
        if not wallet_names and entity.unique_id.startswith("binance_wallet_"):
            continue
        _LOGGER.debug(
            "Removing stale sensor: %s (%s)", entity.entity_id, entity.unique_id
        )
        entity_registry.async_remove(entity.entity_id)

    # --- Create sensors ---
    sensors: list[SensorEntity] = []

    def owned_here(uid: str) -> bool:
        """Market sensors are shared: create one only if it is unregistered
        or already registered under THIS entry (restore on HA restart)."""
        existing_eid = entity_registry.async_get_entity_id("sensor", DOMAIN, uid)
        if existing_eid is None:
            return True
        entity_entry = entity_registry.async_get(existing_eid)
        return bool(
            entity_entry and entity_entry.config_entry_id == config_entry.entry_id
        )

    for pair in futures_pairs:
        if owned_here(f"binance_futures_{pair}"):
            sensors.append(
                BinancePriceSensor(
                    price_coordinator, market_coordinator, pair, "futures"
                )
            )
        if funding_enabled and owned_here(f"binance_futures_funding_{pair}"):
            sensors.append(BinanceFundingRateSensor(market_coordinator, pair))

    for pair in spot_pairs:
        if owned_here(f"binance_spot_{pair}"):
            sensors.append(
                BinancePriceSensor(
                    price_coordinator, market_coordinator, pair, "spot"
                )
            )

    # Wallet sensors — per-account.
    def wallet_sensors(names) -> list[SensorEntity]:
        return [
            BinanceWalletSensor(
                account_coordinator, price_coordinator,
                name, account_name, entry_id, currency,
            )
            for name in names
            for currency in ("btc", "usdt")
        ]

    sensors.extend(wallet_sensors(wallet_names))

    # PnL sensor — per-account.
    sensors.append(BinancePnlSensor(account_coordinator, account_name, entry_id))

    # Margin ratio sensor — per-account.
    sensors.append(
        BinanceMarginRatioSensor(account_coordinator, account_name, entry_id)
    )

    # Per-position sensors — PnL, ROE and liquidation distance.
    def position_sensors(positions) -> list[SensorEntity]:
        return [
            cls(account_coordinator, account_name, entry_id, symbol, side)
            for symbol, side in positions
            for cls in POSITION_SENSOR_CLASSES
        ]

    sensors.extend(position_sensors(open_positions))

    async_add_entities(sensors)

    # Wallets missing at setup (e.g. the first wallet fetch failed) and
    # positions opened while HA runs get their sensors as soon as the
    # coordinator reports them. Closed positions stay (unavailable) until
    # the next reload, when the stale cleanup above removes them.
    known_wallets = set(wallet_names)
    known_positions = set(open_positions)

    @callback
    def _add_new_entities() -> None:
        data = account_coordinator.data or {}
        new_wallets = (
            set(data.get(WALLET_DATA, {})) | set(data.get(WALLET_USD_DATA, {}))
        ) - known_wallets
        new_positions = {
            (pos["symbol"], pos.get("positionSide", "BOTH"))
            for pos in data.get(PNL_DATA, [])
        } - known_positions if positions_enabled else set()
        new_entities = []
        if new_wallets:
            known_wallets.update(new_wallets)
            new_entities += wallet_sensors(sorted(new_wallets))
        if new_positions:
            known_positions.update(new_positions)
            new_entities += position_sensors(sorted(new_positions))
        if new_entities:
            async_add_entities(new_entities)

    config_entry.async_on_unload(
        account_coordinator.async_add_listener(_add_new_entities)
    )


# ======================================================================
# Price Sensor (uses shared price coordinator)
# ======================================================================


class BinancePriceSensor(CoordinatorEntity, SensorEntity):
    """Binance trading pair price sensor.

    With % changes enabled it also carries change_1m … change_1M, computed
    on every price update from the live price and the market coordinator's
    reference prices. They change every tick, so they stay out of the
    recorder.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset(f"change_{w}" for w in CHANGE_WINDOWS)

    def __init__(
        self, coordinator, market_coordinator, symbol: str, market_type: str
    ) -> None:
        super().__init__(coordinator)
        self._market = market_coordinator
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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # New reference prices arrive once a minute on the market coordinator.
        self.async_on_remove(
            self._market.async_add_listener(self._handle_coordinator_update)
        )

    def _changes(self, sym: dict) -> dict:
        """change_* attributes; empty when % changes are not enabled."""
        refs = (
            (self._market.data or {})
            .get(CHANGE_REFS, {})
            .get(self._market_type, {})
            .get(self._symbol)
        )
        if refs is None:
            return {}
        price = _to_float(sym.get("lastPrice"), None)
        changes = {}
        for window in CHANGE_WINDOWS:
            ref = refs.get(window)
            changes[f"change_{window}"] = (
                round((price / ref - 1) * 100, 2) if price and ref else None
            )
        # Rolling 24 h straight from the ticker.
        changes["change_1d"] = _to_float(sym.get("priceChangePercent"), None)
        return changes

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
            **self._changes(sym),
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
# Funding Rate Sensor (uses shared market coordinator)
# ======================================================================


class BinanceFundingRateSensor(CoordinatorEntity, SensorEntity):
    """Current funding rate of a USDⓈ-M perpetual, from premiumIndex."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 4
    _attr_icon = "mdi:cash-sync"
    # Mark / index move every minute; keep them live but out of the recorder.
    _unrecorded_attributes = frozenset({"mark_price", "index_price"})

    def __init__(self, coordinator, symbol: str) -> None:
        super().__init__(coordinator)
        self._symbol = symbol
        self._attr_unique_id = f"binance_futures_funding_{symbol}"
        self._attr_name = f"Binance Futures {symbol} Funding Rate"

    @property
    def _funding(self) -> dict | None:
        return (self.coordinator.data or {}).get(FUNDING_DATA, {}).get(
            self._symbol
        )

    @property
    def available(self) -> bool:
        return super().available and self._funding is not None

    @property
    def native_value(self):
        funding = self._funding
        if funding:
            return round(funding["rate"] * 100, 6)
        return None

    @property
    def extra_state_attributes(self) -> dict:
        funding = self._funding
        if not funding:
            return {}
        hours = funding["interval_hours"]
        next_ms = funding["next_funding_time"]
        return {
            "next_funding_time": (
                datetime.fromtimestamp(next_ms / 1000, tz=UTC).isoformat()
                if next_ms
                else None
            ),
            "funding_interval_hours": hours,
            # Simple (non-compounded) yearly rate at the current funding.
            "annualized_rate": (
                round(funding["rate"] * 100 * 24 / hours * 365, 2)
                if hours
                else None
            ),
            "mark_price": funding["mark_price"],
            "index_price": funding["index_price"],
        }

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, "binance_futures_market")},
            "name": "Binance Futures Market",
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
        if not data:
            return False
        if self._currency == "usdt":
            # Preferred: USDT valuation straight from Binance.
            if self._wallet_name in data.get(WALLET_USD_DATA, {}):
                return True
            # Fallback: BTC balance × cached BTCUSDT reference price.
            if self._wallet_name not in data.get(WALLET_DATA, {}):
                return False
            price_data = self._price_coordinator.data
            return bool(price_data) and price_data.get(BTCUSDT_PRICE) is not None
        return self._wallet_name in data.get(WALLET_DATA, {})

    @property
    def native_value(self):
        data = self.coordinator.data
        if not data:
            return None

        if self._currency == "usdt":
            # Preferred: USDT valuation straight from Binance.
            usd_balance = data.get(WALLET_USD_DATA, {}).get(self._wallet_name)
            if usd_balance is not None:
                return round(usd_balance, 2)
            # Fallback: BTC balance × cached BTCUSDT reference price.
            btc_balance = data.get(WALLET_DATA, {}).get(self._wallet_name)
            if btc_balance is None:
                return None
            price_data = self._price_coordinator.data
            if not price_data:
                return None
            price = price_data.get(BTCUSDT_PRICE)
            if price is None:
                return None
            return round(btc_balance * price, 2)

        return data.get(WALLET_DATA, {}).get(self._wallet_name)

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

        margin_positions = (self.coordinator.data or {}).get(
            MARGIN_DATA, {}
        ).get("positions", {})

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
            margin = margin_positions.get(f"{pos['symbol']}:{side}")
            if margin and margin.get("margin_ratio") is not None:
                attrs[f"{prefix}_margin_ratio"] = margin["margin_ratio"]
        return attrs

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f"binance_account_{self._entry_id}")},
            "name": f"Binance {self._account_name}",
            "manufacturer": "Binance",
            "model": "Wallets",
        }


# ======================================================================
# Futures Margin Ratio Sensor
# ======================================================================


class BinanceMarginRatioSensor(CoordinatorEntity, SensorEntity):
    """Futures account margin ratio — liquidation risk indicator.

    State is the cross-margin ratio Binance shows in the Futures UI:
    maintenance margin / margin balance × 100. Cross positions are
    liquidated when it reaches 100%. Isolated positions carry their own
    per-position ratio, exposed through the attributes.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:gauge"

    def __init__(self, coordinator, account_name: str, entry_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._account_name = account_name
        fmt_account = account_name.lower().replace(" ", "_")

        self._attr_unique_id = f"binance_margin_ratio_{fmt_account}"
        self._attr_name = f"Binance {account_name} Futures Margin Ratio"

    @property
    def _margin(self) -> dict:
        data = self.coordinator.data
        if data:
            return data.get(MARGIN_DATA, {})
        return {}

    @property
    def available(self) -> bool:
        return super().available and bool(self._margin)

    @property
    def native_value(self):
        return self._margin.get("margin_ratio")

    @property
    def extra_state_attributes(self) -> dict:
        margin = self._margin
        if not margin:
            return {}

        attrs = {
            # False when the login has no USDⓈ-M Futures account; the
            # state is then unknown rather than a made-up 0 %.
            "futures_enabled": margin.get("futures_enabled", True),
            "maintenance_margin": margin.get("maint_margin"),
            "margin_balance": margin.get("margin_balance"),
            "total_margin_ratio": margin.get("total_margin_ratio"),
            "total_maintenance_margin": margin.get("total_maint_margin"),
            "total_margin_balance": margin.get("total_margin_balance"),
            "available_balance": margin.get("available_balance"),
        }
        for key, detail in margin.get("positions", {}).items():
            symbol, _, side = key.partition(":")
            prefix = symbol if side == "BOTH" else f"{symbol}_{side}"
            attrs[f"{prefix}_margin_ratio"] = detail["margin_ratio"]
            attrs[f"{prefix}_margin_type"] = detail["margin_type"]
            attrs[f"{prefix}_maintenance_margin"] = detail["maint_margin"]
            attrs[f"{prefix}_margin_balance"] = detail["margin_balance"]
        return attrs

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f"binance_account_{self._entry_id}")},
            "name": f"Binance {self._account_name}",
            "manufacturer": "Binance",
            "model": "Wallets",
        }


# ======================================================================
# Per-Position Sensors (PnL, ROE, liquidation distance)
# ======================================================================


class BinancePositionSensorBase(CoordinatorEntity, SensorEntity):
    """Common plumbing for sensors bound to one open futures position.

    Created dynamically for every open position. When the position is
    closed the sensor turns unavailable; its entity is removed on the
    next integration reload.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _kind: str
    _label: str

    def __init__(
        self,
        coordinator,
        account_name: str,
        entry_id: str,
        symbol: str,
        position_side: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._account_name = account_name
        self._symbol = symbol
        self._side = position_side

        fmt_account = account_name.lower().replace(" ", "_")
        display = (
            symbol
            if position_side == "BOTH"
            else f"{symbol} {position_side.capitalize()}"
        )
        self._attr_unique_id = _position_uid(
            self._kind, fmt_account, symbol, position_side
        )
        self._attr_name = f"Binance {account_name} {display} {self._label}"

    @property
    def _position(self) -> dict | None:
        for pos in (self.coordinator.data or {}).get(PNL_DATA, []):
            if (
                pos["symbol"] == self._symbol
                and pos.get("positionSide", "BOTH") == self._side
            ):
                return pos
        return None

    @property
    def available(self) -> bool:
        return super().available and self._position is not None

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, f"binance_account_{self._entry_id}")},
            "name": f"Binance {self._account_name}",
            "manufacturer": "Binance",
            "model": "Wallets",
        }


class BinancePositionPnlSensor(BinancePositionSensorBase):
    """Unrealized PnL (USD) of one open futures position."""

    _kind = "pnl"
    _label = "Position PnL"
    _attr_native_unit_of_measurement = "USD"
    _attr_icon = "mdi:chart-line-variant"

    @property
    def native_value(self):
        pos = self._position
        return round(pos["unRealizedProfit"], 2) if pos else None

    @property
    def extra_state_attributes(self) -> dict:
        pos = self._position
        if not pos:
            return {}
        return {
            "amount": pos["positionAmt"],
            "entry_price": pos["entryPrice"],
            "mark_price": pos["markPrice"],
            "leverage": pos["leverage"],
            "margin_type": pos["marginType"],
            "liquidation_price": pos["liquidationPrice"],
            "position_side": self._side,
        }


class BinancePositionRoeSensor(BinancePositionSensorBase):
    """ROE % of one open futures position (PnL over initial margin)."""

    _kind = "roi"
    _label = "Position ROE"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:percent-outline"

    @property
    def native_value(self):
        pos = self._position
        return _position_roe(pos) if pos else None


class BinancePositionLiquidationSensor(BinancePositionSensorBase):
    """How far (%) the mark price is from this position's liquidation price."""

    _kind = "liq_distance"
    _label = "Liquidation Distance"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:shield-alert-outline"

    @property
    def native_value(self):
        pos = self._position
        return _liquidation_distance(pos) if pos else None

    @property
    def extra_state_attributes(self) -> dict:
        pos = self._position
        if not pos:
            return {}
        return {
            "liquidation_price": pos["liquidationPrice"],
            "mark_price": pos["markPrice"],
        }


POSITION_SENSOR_CLASSES = (
    BinancePositionPnlSensor,
    BinancePositionRoeSensor,
    BinancePositionLiquidationSensor,
)
