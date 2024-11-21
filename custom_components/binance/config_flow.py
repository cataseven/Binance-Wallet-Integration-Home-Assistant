import voluptuous as vol
import aiohttp
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from .const import DOMAIN, CONF_API_KEY, CONF_API_SECRET, CONF_PAIRS, CONF_SPOT_PAIRS, CONF_FUTURES_PAIRS

class BinanceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            return self.async_create_entry(title="Binance", data=user_input)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://fapi.binance.com/fapi/v2/ticker/price") as response:
                    data = await response.json()
                    futures_symbols = sorted([item["symbol"] for item in data])
                async with session.get("https://api.binance.com/api/v3/ticker/24hr") as response:
                    data = await response.json()
                    spot_symbols = sorted([item["symbol"] for item in data if float(item["weightedAvgPrice"]) > 0])
        except aiohttp.ClientError as err:
            errors["base"] = "cannot_connect"
            _LOGGER.error(f"Binance API'ye bağlanırken hata oluştu: {err}")
        except Exception as err:
            errors["base"] = "unknown"
            _LOGGER.exception(f"Beklenmeyen hata: {err}")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                    vol.Required(CONF_API_SECRET): str,
                    vol.Optional(CONF_FUTURES_PAIRS, default=[]): cv.multi_select(futures_symbols),
                    vol.Optional(CONF_SPOT_PAIRS, default=[]): cv.multi_select(spot_symbols),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        "Get the options flow."
        return BinanceOptionsFlowHandler(config_entry)

class BinanceOptionsFlowHandler(config_entries.OptionsFlow):
    "Handle Binance options."

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://fapi.binance.com/fapi/v2/ticker/price") as response:
                    data = await response.json()
                    futures_symbols = sorted([item["symbol"] for item in data])

                async with session.get("https://api.binance.com/api/v3/ticker/24hr") as response:
                    data = await response.json()
                    spot_symbols = sorted([item["symbol"] for item in data if float(item["weightedAvgPrice"]) > 0])
        except aiohttp.ClientError as err:
            _LOGGER.error(f"Binance API'ye bağlanırken hata oluştu: {err}")
            return self.async_abort(reason="cannot_connect")
        except Exception as err:
            _LOGGER.exception(f"Beklenmeyen hata: {err}")
            return self.async_abort(reason="unknown")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_FUTURES_PAIRS, default=self.config_entry.options.get(CONF_FUTURES_PAIRS, [])
                    ): cv.multi_select(futures_symbols),
                    vol.Optional(
                        CONF_SPOT_PAIRS, default=self.config_entry.options.get(CONF_SPOT_PAIRS, [])
                    ): cv.multi_select(spot_symbols),
                }
            ),
        )
