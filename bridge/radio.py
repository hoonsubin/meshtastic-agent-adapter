"""
Meshtastic radio - async wrapper around one node, over any transport.

The bridge holds one radio: it opens the node through its transport, turns
meshtastic pubsub events into callbacks, sends text with optional delivery
acknowledgement, and tracks routing ACK/NAK responses.

How the node is reached (USB serial today, Bluetooth later) lives in
``bridge.transport``; this module only knows what to do with the node once it is
open, so a new transport needs no change here.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

from pubsub import pub

from .transport import Transport

logger = logging.getLogger(__name__)

# How long to wait for a routing ACK before declaring a unicast unconfirmed
DEFAULT_ACK_TIMEOUT = 8.0
ACK_POLL_INTERVAL = 0.25


class MeshtasticRadio:
    """
    Async wrapper around a node reached through a Transport.

    Provides:
    - Connection through the transport (any pre-open hook it defines)
    - Async message sending with optional delivery acknowledgement
    - Callback-based message receiving (text messages, and all packets)
    - Reconnection logic
    """

    def __init__(
        self,
        transport: Transport,
        hop_limit: int = 3,
        on_receive: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_packet: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.transport = transport
        self.hop_limit = hop_limit
        self.on_receive = on_receive
        # Fired for EVERY received packet (any portnum); used for RX liveness
        self.on_packet = on_packet

        self._interface: Optional[Any] = None
        self._connected = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        # Sent packet id -> None while pending, then the routing errorReason
        # ("NONE" means the destination acknowledged)
        self._pending_acks: Dict[int, Optional[str]] = {}

    async def connect(self) -> bool:
        """Open the node through the transport."""
        logger.info(f"Connecting to Meshtastic node via {self.transport.describe()}...")

        try:
            loop = asyncio.get_event_loop()
            self._interface = await loop.run_in_executor(
                None, self._connect_blocking
            )
            self._connected = True
            self._reconnect_delay = 1.0  # Reset backoff
            logger.info(f"Connected via {self.transport.describe()}")
            return True

        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            self._connected = False
            return False

    def _connect_blocking(self) -> Any:
        """Blocking connect, run in a worker thread."""
        self.transport.prepare()
        interface = self.transport.open()

        # Ping every inbound packet (any transport) through one subscription
        pub.subscribe(self._handle_receive, "meshtastic.receive")
        logger.info("Receive callback registered via pubsub")
        return interface

    async def disconnect(self) -> None:
        """Close the node through the transport."""
        interface = self._interface
        if interface is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: self.transport.close(interface))
        self._connected = False
        logger.info("Disconnected from Meshtastic node")

    async def send_text(
        self,
        text: str,
        destination: Optional[str] = None,
        channel: int = 0,
        want_ack: bool = True,
        ack_timeout: float = DEFAULT_ACK_TIMEOUT,
    ) -> bool:
        """
        Send a text message via Meshtastic.

        Args:
            text: Message text to send
            destination: Target node ID (e.g. "!68916e4c") or None for broadcast
            channel: Channel index (default 0)
            want_ack: Request a delivery ACK for unicast messages
            ack_timeout: Seconds to wait for that ACK

        Returns:
            True if the message was handed to the radio. Delivery is only
            confirmed for unicast messages that come back acknowledged; a missing
            ACK is logged as a warning (it means "unconfirmed", not "failed").
        """
        if not self._connected or self._interface is None:
            logger.error("Not connected")
            return False

        try:
            # Use '^all' for broadcast if destination is None
            dest = destination if destination is not None else "^all"
            is_broadcast = dest in ("^all", "!ffffffff")
            request_ack = bool(want_ack) and not is_broadcast

            loop = asyncio.get_event_loop()
            packet = await loop.run_in_executor(
                None,
                lambda: self._interface.sendText(
                    text,
                    destinationId=dest,
                    channelIndex=channel,
                    hopLimit=self.hop_limit,
                    wantAck=request_ack,
                ),
            )
            logger.info(f"Sent message to {destination or 'broadcast'}: {text[:50]}...")

            packet_id = getattr(packet, "id", None)
            if request_ack and packet_id:
                await self._confirm_delivery(packet_id, dest, ack_timeout)

            return True

        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False

    async def _confirm_delivery(
        self, packet_id: int, destination: str, ack_timeout: float
    ) -> None:
        """
        Wait for the routing ACK of a unicast packet and report the outcome.

        A LoRa handset that never ACKs is the signature of a deaf receiver or a
        lost packet; without this the bridge would log a cheerful "Sent" for a
        message nobody received.
        """
        self._pending_acks[packet_id] = None
        deadline = time.monotonic() + max(0.0, ack_timeout)
        try:
            while time.monotonic() < deadline:
                result = self._pending_acks.get(packet_id)
                if result is not None:
                    if result == "NONE":
                        logger.debug(f"Delivery confirmed by {destination}")
                    else:
                        logger.warning(
                            f"Delivery NAK from {destination} for packet "
                            f"{packet_id}: {result}"
                        )
                    return
                await asyncio.sleep(ACK_POLL_INTERVAL)

            logger.warning(
                f"No routing ACK from {destination} within {ack_timeout:.0f}s "
                f"(packet {packet_id}); the message may not have been received"
            )
        finally:
            self._pending_acks.pop(packet_id, None)

    def _handle_receive(self, packet: Dict[str, Any], interface) -> None:
        """Callback for received packets (runs in the meshtastic reader thread)."""
        try:
            decoded = packet.get("decoded") or {}

            # Routing responses (ACK/NAK) for packets we sent
            request_id = decoded.get("requestId")
            if request_id is not None and request_id in self._pending_acks:
                routing = decoded.get("routing") or {}
                self._pending_acks[request_id] = routing.get("errorReason", "NONE")

            # Any inbound packet is evidence the radio can hear (RX liveness)
            if self.on_packet:
                self.on_packet(packet)

            # Only process text messages from here on
            portnum = decoded.get("portnum")
            if portnum != "TEXT_MESSAGE_APP":
                return

            from_id = packet.get("fromId")
            to_id = packet.get("toId")
            text = decoded.get("text", "")
            channel = packet.get("channel", 0)

            logger.info(f"Received from {from_id}: {text[:50]}...")

            # Call user callback
            if self.on_receive:
                self.on_receive(
                    {
                        "from_id": from_id,
                        "to_id": to_id,
                        "text": text,
                        "channel": channel,
                        "packet": packet,
                    }
                )

        except Exception as e:
            logger.error(f"Error handling receive: {e}", exc_info=True)

    async def reconnect(self) -> bool:
        """Attempt to reconnect with exponential backoff."""
        logger.info(f"Reconnecting in {self._reconnect_delay:.1f}s...")
        await asyncio.sleep(self._reconnect_delay)

        # Exponential backoff
        self._reconnect_delay = min(
            self._reconnect_delay * 2, self._max_reconnect_delay
        )

        return await self.connect()

    @property
    def my_node_num(self) -> Optional[int]:
        """This node's own numeric id, or None before the interface is up."""
        if self._interface is None:
            return None
        return getattr(getattr(self._interface, "myInfo", None), "my_node_num", None)

    @property
    def my_node_id(self) -> Optional[str]:
        """This node's own id as a string, e.g. '!02e72ba8'."""
        num = self.my_node_num
        return f"!{num:08x}" if num else None

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._connected and self._interface is not None
