import asyncio
import logging
import time
import hmac
import hashlib
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    FUTURES_API_URL,
    SPOT_API_URL,
    FUTURES_DATA,
    SPOT_DATA,
    WALLET_DATA,
    BTCUSDT_PRICE,
)

_LOGGER = logging.getLogger(__name__)


async def options_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _LOGGER.info("Binance configuration options updated, reloading integration.")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    coordinator = BinanceDataUpdateCoordinator(
        hass,
        api_key=entry.data[CONF_API_KEY],
        api_secret=entry.data[CONF_API_SECRET],
        update_interval=entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(options_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


class BinanceDataUpdateCoordinator(DataUpdateCoordinator):

    def __init__(self, hass, api_key, api_secret, update_interval):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = aiohttp.ClientSession()

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_get_btcusdt_price(self):
        url = f"{SPOT_API_URL}/api/v3/ticker/price?symbol=BTCUSDT"
        async with self.session.get(url) as response:
            response.raise_for_status()
            data = await response.json()
            return float(data['price'])

    async def _async_update_data(self):
        try:
            async with asyncio.timeout(30):
                futures_res, spot_res, wallet_res, btcusdt_price = await asyncio.gather(
                    self._async_get_futures_data(),
                    self._async_get_spot_data(),
                    self._async_get_wallet_balance(),
                    self._async_get_btcusdt_price(),
                )

                processed_wallets = {
                    item["walletName"]: float(item["balance"])
                    for item in wallet_res
                }

                return {
                    FUTURES_DATA: {item["symbol"]: item for item in futures_res},
                    SPOT_DATA: {item["symbol"]: item for item in spot_res},
                    WALLET_DATA: processed_wallets,
                    BTCUSDT_PRICE: btcusdt_price,
                }
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except asyncio.TimeoutError as err:
            raise UpdateFailed("Timeout while connecting to API") from err
        except Exception as err:
            raise UpdateFailed(f"An unexpected error occurred: {err}") from err

    async def _async_get_futures_data(self):
        url = f"{FUTURES_API_URL}/fapi/v1/ticker/24hr"
        async with self.session.get(url) as response:
            response.raise_for_status()
            return await response.json()

    async def _async_get_spot_data(self):
        url = f"{SPOT_API_URL}/api/v3/ticker/24hr"
        async with self.session.get(url) as response:
            response.raise_for_status()
            return await response.json()

    async def _async_get_wallet_balance(self):
        url = f"{SPOT_API_URL}/sapi/v1/asset/wallet/balance"
        timestamp = int(time.time() * 1000)
        headers = {"X-MBX-APIKEY": self.api_key}

        query_string = f"timestamp={timestamp}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        full_url = f"{url}?{query_string}&signature={signature}"

        async with self.session.get(full_url, headers=headers) as response:
            response.raise_for_status()
            return await response.json()
