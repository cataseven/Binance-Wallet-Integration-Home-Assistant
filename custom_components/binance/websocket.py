"""WebSocket manager for real-time Binance price streams."""

import asyncio
import json
import logging
from typing import Any

import aiohttp

from .const import (
    FUTURES_DATA,
    FUTURES_WS_URL,
    SPOT_DATA,
    SPOT_WS_URL,
    WS_MAX_STREAMS_PER_CONNECTION,
    WS_RECONNECT_DELAY,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_ws_ticker(ws_data: dict) -> dict[str, Any]:
    """Convert WebSocket 24hrTicker fields to REST-compatible format."""
    return {
        "symbol": ws_data.get("s", ""),
        "lastPrice": ws_data.get("c", "0"),
        "priceChangePercent": ws_data.get("P", "0"),
        "highPrice": ws_data.get("h", "0"),
        "lowPrice": ws_data.get("l", "0"),
        "volume": ws_data.get("v", "0"),
        "quoteVolume": ws_data.get("q", "0"),
    }


def _build_stream_urls(pairs: list[str], base_url: str) -> list[str]:
    """Build WebSocket URLs, chunking if pairs exceed per-connection limit."""
    if not pairs:
        return []

    urls = []
    for i in range(0, len(pairs), WS_MAX_STREAMS_PER_CONNECTION):
        chunk = pairs[i : i + WS_MAX_STREAMS_PER_CONNECTION]
        streams = "/".join(f"{symbol.lower()}@ticker" for symbol in chunk)
        urls.append(f"{base_url}?streams={streams}")
    return urls


class BinanceWebSocketManager:
    """Manages WebSocket connections to Binance for real-time price data."""

    def __init__(self, hass, coordinator, session: aiohttp.ClientSession) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._session = session
        self._tasks: list[asyncio.Task] = []
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(
        self, spot_pairs: list[str], futures_pairs: list[str]
    ) -> None:
        """Start WebSocket connections for the given pairs."""
        if self._running:
            await self.stop()

        self._running = True

        spot_urls = _build_stream_urls(spot_pairs, SPOT_WS_URL)
        futures_urls = _build_stream_urls(futures_pairs, FUTURES_WS_URL)

        for url in spot_urls:
            task = self._hass.async_create_background_task(
                self._listen(url, SPOT_DATA),
                f"binance_spot_ws_{len(self._tasks)}",
            )
            self._tasks.append(task)

        for url in futures_urls:
            task = self._hass.async_create_background_task(
                self._listen(url, FUTURES_DATA),
                f"binance_futures_ws_{len(self._tasks)}",
            )
            self._tasks.append(task)

        stream_count = len(spot_pairs) + len(futures_pairs)
        _LOGGER.info(
            "Binance WebSocket started: %d stream(s) across %d connection(s)",
            stream_count,
            len(self._tasks),
        )

    async def stop(self) -> None:
        """Stop all WebSocket connections."""
        self._running = False
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        _LOGGER.info("Binance WebSocket connections stopped")

    async def _listen(self, url: str, data_key: str) -> None:
        """Maintain a persistent WebSocket connection with auto-reconnect."""
        consecutive_errors = 0

        while self._running:
            try:
                async with self._session.ws_connect(
                    url, heartbeat=20, receive_timeout=30
                ) as ws:
                    consecutive_errors = 0
                    _LOGGER.debug("WebSocket connected: %s", data_key)

                    async for msg in ws:
                        if not self._running:
                            break

                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_message(msg.data, data_key)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            _LOGGER.warning(
                                "WebSocket error on %s: %s", data_key, ws.exception()
                            )
                            break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        ):
                            break

            except asyncio.CancelledError:
                return
            except Exception:
                consecutive_errors += 1
                delay = min(
                    WS_RECONNECT_DELAY * (2 ** (consecutive_errors - 1)),
                    300,  # max 5 min
                )
                _LOGGER.warning(
                    "WebSocket %s disconnected, reconnecting in %ds "
                    "(attempt %d)",
                    data_key,
                    delay,
                    consecutive_errors,
                    exc_info=consecutive_errors <= 1,
                )
                await asyncio.sleep(delay)

    def _handle_message(self, raw: str, data_key: str) -> None:
        """Parse a WebSocket message and update coordinator data."""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        ws_data = payload.get("data")
        if not ws_data or not isinstance(ws_data, dict):
            return

        ticker = _normalize_ws_ticker(ws_data)
        symbol = ticker.get("symbol")
        if not symbol:
            return

        coord_data = self._coordinator.data
        if not coord_data or data_key not in coord_data:
            return

        # Only update symbols we're tracking.
        if symbol not in coord_data[data_key]:
            return

        coord_data[data_key][symbol] = ticker
        self._coordinator.async_set_updated_data(coord_data)
