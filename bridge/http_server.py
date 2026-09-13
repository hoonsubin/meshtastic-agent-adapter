"""
HTTP Server - Exposes API for agents to send messages to mesh

Endpoints:
- POST /send: Send message to specific node
- POST /callback: Send response back to originating node
- GET /health: Health check
"""

import asyncio
import inspect
import logging
import time
from typing import Optional, Callable, Awaitable

from aiohttp import web

from .text import truncate_bytes

logger = logging.getLogger(__name__)

# Conservative LoRa text budget in bytes for agent-initiated sends
LORA_MAX_BYTES = 200


class BridgeHTTPServer:
    """HTTP server for agent-to-mesh communication."""

    def __init__(
        self,
        host: str,
        port: int,
        send_fn: Callable[[str, str, int], Awaitable[bool]],
        health_fn: Optional[Callable[[], dict]] = None,
    ):
        """
        Args:
            host: Bind address
            port: Bind port
            send_fn: Async function(destination_id, message, channel) -> bool
            health_fn: Optional function() -> dict for health status
        """
        self.host = host
        self.port = port
        self.send_fn = send_fn
        self.health_fn = health_fn

        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._start_time: Optional[float] = None

    async def start(self) -> None:
        """Start HTTP server."""
        self._app = web.Application()
        self._app.router.add_post("/send", self._handle_send)
        self._app.router.add_post("/callback", self._handle_callback)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        self._start_time = time.time()
        logger.info(f"HTTP server listening on http://{self.host}:{self.port}")

    async def stop(self) -> None:
        """Stop HTTP server."""
        if self._runner:
            await self._runner.cleanup()
        logger.info("HTTP server stopped")

    async def _handle_send(self, request: web.Request) -> web.Response:
        """
        POST /send
        Body: {"destination_id": "!68916e4c", "message": "...", "channel": 0}
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "Invalid JSON"}, status=400
            )

        destination_id = data.get("destination_id")
        message = data.get("message")
        channel = data.get("channel", 0)

        if not message:
            return web.json_response(
                {"status": "error", "message": "message is required"}, status=400
            )

        # Truncate to the LoRa budget (byte-safe, shared helper)
        message = truncate_bytes(message, LORA_MAX_BYTES)

        logger.info(f"Send request to {destination_id or 'broadcast'}: {message[:50]}...")

        success = await self.send_fn(destination_id, message, channel)

        if success:
            return web.json_response(
                {
                    "status": "sent",
                    "destination": destination_id or "broadcast",
                }
            )
        else:
            return web.json_response(
                {"status": "error", "message": "Failed to send"}, status=500
            )

    async def _handle_callback(self, request: web.Request) -> web.Response:
        """
        POST /callback
        Body: {"destination_id": "!68916e4c", "response": "...", "channel": 0}

        Same as /send but semantically for responses to inbound messages.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "Invalid JSON"}, status=400
            )

        destination_id = data.get("destination_id")
        response = data.get("response")
        channel = data.get("channel", 0)

        if not response:
            return web.json_response(
                {"status": "error", "message": "response is required"}, status=400
            )

        # Truncate to the LoRa budget (byte-safe, shared helper)
        response = truncate_bytes(response, LORA_MAX_BYTES)

        logger.info(
            f"Callback to {destination_id or 'broadcast'}: {response[:50]}..."
        )

        success = await self.send_fn(destination_id, response, channel)

        if success:
            return web.json_response(
                {
                    "status": "delivered",
                    "destination": destination_id or "broadcast",
                }
            )
        else:
            return web.json_response(
                {"status": "error", "message": "Failed to deliver"}, status=500
            )

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET /health"""
        uptime = time.time() - self._start_time if self._start_time else 0

        health_data = {
            "status": "ok",
            "uptime": int(uptime),
        }

        if self.health_fn:
            try:
                if inspect.iscoroutinefunction(self.health_fn):
                    extra = await self.health_fn()
                else:
                    extra = self.health_fn()
                health_data.update(extra)
            except Exception as e:
                logger.error(f"Health check error: {e}")
                health_data["status"] = "degraded"

        return web.json_response(health_data)
