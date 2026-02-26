"""Config flow for Binance integration."""

import hashlib
import hmac
import logging
import time

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCOUNT_NAME,
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_FUTURES_PAIRS,
    CONF_SPOT_PAIRS,
    CONF_UPDATE_INTERVAL,
    CONF_USE_WEBSOCKET,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_USE_WEBSOCKET,
    DOMAIN,
    FUTURES_API_URL,
    SPOT_API_URL,
)

_LOGGER = logging.getLogger(__name__)

# --- Symbol cache ---
_symbol_cache: dict = {"futures": [], "spot": [], "ts": 0.0}
_CACHE_TTL = 300  # 5 minutes


async def _get_symbols(session: aiohttp.ClientSession) -> tuple[list[str], list[str]]:
    """Fetch and cache available trading symbols from Binance."""
    now = time.monotonic()
    if _symbol_cache["ts"] > now - _CACHE_TTL and _symbol_cache["futures"]:
        return _symbol_cache["futures"], _symbol_cache["spot"]

    async with session.get(f"{FUTURES_API_URL}/fapi/v2/ticker/price") as resp:
        resp.raise_for_status()
        futures_data = await resp.json()
        futures_symbols = sorted(item["symbol"] for item in futures_data)

    async with session.get(f"{SPOT_API_URL}/api/v3/ticker/24hr") as resp:
        resp.raise_for_status()
        spot_data = await resp.json()
        spot_symbols = sorted(
            item["symbol"]
            for item in spot_data
            if float(item.get("weightedAvgPrice", 0)) > 0
        )

    _symbol_cache.update({"futures": futures_symbols, "spot": spot_symbols, "ts": now})
    return futures_symbols, spot_symbols


async def _validate_api_credentials(
    session: aiohttp.ClientSession, api_key: str, api_secret: str
) -> str | None:
    """Test API credentials. Returns error key or None on success."""
    try:
        timestamp = int(time.time() * 1000)
        query_string = f"timestamp={timestamp}&recvWindow=10000"
        signature = hmac.new(
            api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        url = (
            f"{SPOT_API_URL}/sapi/v1/asset/wallet/balance"
            f"?{query_string}&signature={signature}"
        )
        headers = {"X-MBX-APIKEY": api_key}

        async with session.get(url, headers=headers) as resp:
            if resp.status == 401:
                return "invalid_api_key"
            if resp.status == 403:
                return "api_permission_denied"
            resp.raise_for_status()
            return None
    except aiohttp.ClientError:
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error validating Binance API credentials")
        return "unknown"


class BinanceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Binance."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        session = async_get_clientsession(self.hass)

        if user_input is not None:
            error = await _validate_api_credentials(
                session,
                user_input[CONF_API_KEY],
                user_input[CONF_API_SECRET],
            )
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=f"Binance ({user_input[CONF_ACCOUNT_NAME]})",
                    data=user_input,
                )

        futures_symbols: list[str] = []
        spot_symbols: list[str] = []

        try:
            futures_symbols, spot_symbols = await _get_symbols(session)
        except aiohttp.ClientError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception fetching symbols")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_NAME): str,
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
        """Return the options flow handler."""
        return BinanceOptionsFlowHandler()


class BinanceOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Binance options flow.

    Note: self.config_entry is automatically set by HA (2024.4+).
    Do NOT override __init__ to accept config_entry.
    """

    async def async_step_init(self, user_input=None):
        """Handle options update."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        session = async_get_clientsession(self.hass)

        try:
            futures_symbols, spot_symbols = await _get_symbols(session)
        except aiohttp.ClientError:
            return self.async_abort(reason="cannot_connect")
        except Exception:
            _LOGGER.exception("Unexpected exception fetching symbols")
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
        current_ws = self.config_entry.options.get(
            CONF_USE_WEBSOCKET, DEFAULT_USE_WEBSOCKET
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
                    vol.Required(
                        CONF_USE_WEBSOCKET, default=current_ws
                    ): bool,
                }
            ),
        )
