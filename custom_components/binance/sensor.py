from homeassistant.components.sensor import SensorEntity
from .const import DOMAIN, CONF_API_KEY, CONF_API_SECRET, CONF_FUTURES_PAIRS, CONF_SPOT_PAIRS
import logging
import aiohttp
import hashlib
import hmac
import time
import asyncio

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    "Set up Binance sensors based on a config entry."
    api_key = config_entry.data[CONF_API_KEY]
    api_secret = config_entry.data[CONF_API_SECRET]

    futures_pairs = config_entry.data.get(CONF_FUTURES_PAIRS, []) + config_entry.options.get(CONF_FUTURES_PAIRS, [])
    spot_pairs = config_entry.data.get(CONF_SPOT_PAIRS, []) + config_entry.options.get(CONF_SPOT_PAIRS, [])

    sensors = []
    
    for pair in futures_pairs:
        sensors.append(BinanceFuturesPriceSensor(api_key, api_secret, pair))

    for pair in spot_pairs:
        sensors.append(BinanceSpotPriceSensor(api_key, api_secret, pair))

    sensors.append(BinanceWalletBalanceSensor(api_key, api_secret))

    async_add_entities(sensors, update_before_add=True)

    _LOGGER.debug(f"Created {len(sensors)} sensors for pairs: {futures_pairs + spot_pairs}")

    async def update_sensors():
        while True:
            for sensor in sensors:
                await sensor.async_update()
            await asyncio.sleep(30)

    hass.loop.create_task(update_sensors())

class BinanceFuturesPriceSensor(SensorEntity):

    def __init__(self, api_key, api_secret, symbol):
        "Initialize the sensor."
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbol = symbol
        self._state = None
        self._price_change = None

    @property
    def name(self):
        return f"Binance Futures {self._symbol} Price"

    @property
    def native_value(self):
        return self._state

    @property
    def native_unit_of_measurement(self):
        if self._symbol.endswith("USDT"):
            return "USDT"
        elif self._symbol.endswith("DUSD"):
            return "FDUSD"
        elif self._symbol.endswith("USDC"):
            return "USDC"
        elif self._symbol.endswith("TUSD"):
            return "TUSD"
        elif self._symbol.endswith("DOGE"):
            return "DOGE"
        elif self._symbol.endswith("AEUR"):
            return "AEUR"
        elif self._symbol.endswith("EURI"):
            return "EURI"
        else:
            return self._symbol[-3:]

    @property
    def icon(self):
        "Return the icon of the sensor."
        return "mdi:currency-usd"

    @property
    def extra_state_attributes(self):
        return {
            "price_change": self._price_change,
        }

    async def async_update(self):
        try:
            _LOGGER.debug(f"Fetching data for {self._symbol}...")
            price_data = await self._fetch_binance_data(self._symbol)
            self._state = price_data.get("price")
            self._price_change = price_data.get("priceChangePercent")
            _LOGGER.debug(f"Fetched data for {self._symbol}: price={self._state}, change={self._price_change}")
        except Exception as e:
            _LOGGER.error(f"Error fetching data for {self._symbol}: {e}")
            self._state = None
            self._price_change = None

    async def _fetch_binance_data(self, symbol):
        url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}"
        headers = {"X-MBX-APIKEY": self._api_key}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return {
                        "price": float(data["lastPrice"]),
                        "priceChangePercent": float(data["priceChangePercent"]),
                    }
                else:
                    raise Exception(f"Failed to fetch data for {symbol}, status: {response.status}")

class BinanceSpotPriceSensor(SensorEntity):

    def __init__(self, api_key, api_secret, symbol):
        "Initialize the sensor."
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbol = symbol
        self._state = None
        self._price_change = None

    @property
    def name(self):
        return f"Binance Spot {self._symbol} Price"

    @property
    def native_value(self):
        return self._state

    @property
    def native_unit_of_measurement(self):
        if self._symbol.endswith("USDT"):
            return "USDT"
        elif self._symbol.endswith("DUSD"):
            return "FDUSD"
        elif self._symbol.endswith("USDC"):
            return "USDC"
        elif self._symbol.endswith("TUSD"):
            return "TUSD"
        elif self._symbol.endswith("DOGE"):
            return "DOGE"
        elif self._symbol.endswith("AEUR"):
            return "AEUR"
        elif self._symbol.endswith("EURI"):
            return "EURI"
        else:
            return self._symbol[-3:]

    @property
    def icon(self):
        return "mdi:currency-usd"

    @property
    def extra_state_attributes(self):
        return {
            "price_change": self._price_change,
        }

    async def async_update(self):
        try:
            _LOGGER.debug(f"Fetching data for {self._symbol}...")
            price_data = await self._fetch_binance_data(self._symbol)
            self._state = price_data.get("price")
            self._price_change = price_data.get("priceChangePercent")
            _LOGGER.debug(f"Fetched data for {self._symbol}: price={self._state}, change={self._price_change}")
        except Exception as e:
            _LOGGER.error(f"Error fetching data for {self._symbol}: {e}")
            self._state = None
            self._price_change = None

    async def _fetch_binance_data(self, symbol):
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        headers = {"X-MBX-APIKEY": self._api_key}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return {
                        "price": float(data["lastPrice"]),
                        "priceChangePercent": float(data["priceChangePercent"]),
                    }
                else:
                    raise Exception(f"Failed to fetch data for {symbol}, status: {response.status}")

class BinanceWalletBalanceSensor(SensorEntity):

    def __init__(self, api_key, api_secret):
        "Initialize the sensor."
        self._api_key = api_key
        self._api_secret = api_secret
        self._state = None
        self._attributes = {}

    @property
    def name(self):
        return "Binance Wallet Balance"

    @property
    def native_value(self):
        return sum(self._attributes.values()) if self._attributes else None

    @property
    def native_unit_of_measurement(self):
        return "USD"

    @property
    def icon(self):
        return "mdi:currency-usd"

    @property
    def extra_state_attributes(self):
        return self._attributes

    async def async_update(self):
        try:
            _LOGGER.debug(f"Fetching Wallet Balance...")
            balance_data = await self._fetch_binance_wallet_balance()

            self._attributes = {}
            for balance in balance_data:
                self._attributes[balance["walletName"]] = float(balance["balance"])

            _LOGGER.debug(f"Fetched Wallet Balance: {self._attributes}")
        except Exception as e:
            _LOGGER.error(f"Error fetching Wallet Balance: {e}")
            self._state = None
            self._attributes = {}

    async def _fetch_binance_wallet_balance(self):
        url = "https://api.binance.com/sapi/v1/asset/wallet/balance"
        timestamp = int(time.time() * 1000)
        headers = {"X-MBX-APIKEY": self._api_key}

        query_string = f"timestamp={timestamp}"
        signature = hmac.new(self._api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

        url += f"?{query_string}&signature={signature}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                else:
                    raise Exception(f"Failed to fetch Wallet Balance, status: {response.status}")
