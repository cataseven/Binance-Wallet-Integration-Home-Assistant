import logging
import voluptuous as vol
import aiohttp

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_FUTURES_PAIRS,
    CONF_SPOT_PAIRS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    FUTURES_API_URL,
    SPOT_API_URL,
)

_LOGGER = logging.getLogger(__name__)


async def _get_binance_symbols():
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{FUTURES_API_URL}/fapi/v2/ticker/price") as response:
            response.raise_for_status()
            data = await response.json()
            futures_symbols = sorted([item["symbol"] for item in data])

        async with session.get(f"{SPOT_API_URL}/api/v3/ticker/24hr") as response:
            response.raise_for_status()
            data = await response.json()
            spot_symbols = sorted(
                [
                    item["symbol"]
                    for item in data
                    if float(item.get("weightedAvgPrice", 0)) > 0
                ]
            )

    return futures_symbols, spot_symbols


class BinanceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            return self.async_create_entry(title="Binance", data=user_input)

        try:
            futures_symbols, spot_symbols = await _get_binance_symbols()
        except aiohttp.ClientError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                    vol.Required(CONF_API_SECRET): str,
                    vol.Optional(CONF_FUTURES_PAIRS, default=[]): cv.multi_select(
                        futures_symbols
                    ),
                    vol.Optional(CONF_SPOT_PAIRS, default=[]): cv.multi_select(
                        spot_symbols
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BinanceOptionsFlowHandler(config_entry)


class BinanceOptionsFlowHandler(config_entries.OptionsFlow):

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        try:
            futures_symbols, spot_symbols = await _get_binance_symbols()
        except aiohttp.ClientError:
            return self.async_abort(reason="cannot_connect")
        except Exception:
            _LOGGER.exception("Unexpected exception")
            return self.async_abort(reason="unknown")

        current_futures = self.config_entry.options.get(
            CONF_FUTURES_PAIRS, self.config_entry.data.get(CONF_FUTURES_PAIRS, [])
        )
        current_spot = self.config_entry.options.get(
            CONF_SPOT_PAIRS, self.config_entry.data.get(CONF_SPOT_PAIRS, [])
        )
        current_interval = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_FUTURES_PAIRS, default=current_futures
                    ): cv.multi_select(futures_symbols),
                    vol.Optional(
                        CONF_SPOT_PAIRS, default=current_spot
                    ): cv.multi_select(spot_symbols),
                    vol.Required(
                        CONF_UPDATE_INTERVAL, default=current_interval
                    ): vol.All(vol.Coerce(int), vol.Range(min=10)),
                }
            ),
        )
