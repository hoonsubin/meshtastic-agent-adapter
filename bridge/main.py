"""
Meshtastic Bridge - Main entry point

Orchestrates:
- Serial I/O (Meshtastic node)
- HTTP server (agent API)
- HTTP client (agent forwarding)
"""

import asyncio
import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from .config import Config, is_broadcast_id, normalize_node_id
from .radio import MeshtasticRadio
from .transport import build_transport
from .http_server import BridgeHTTPServer
from .http_client import AgentClient

logger = logging.getLogger(__name__)


class MeshtasticBridge:
    """Main bridge orchestrator."""

    def __init__(self, config: Config):
        self.config = config
        self.radio: Optional[MeshtasticRadio] = None
        self.http_server: Optional[BridgeHTTPServer] = None
        self.agent_client: Optional[AgentClient] = None
        self._shutdown_event = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # RX liveness: updated for every received packet, any type
        self._last_packet_at: Optional[float] = None
        self._packet_count = 0
        self._own_packet_count = 0
        # Rate limit for rejection logs: node id -> monotonic time last logged
        self._rejected_logged: dict = {}

    async def start(self) -> None:
        """Start all components."""
        logger.info("Starting Meshtastic Bridge...")
        
        # Capture the event loop for cross-thread scheduling
        self._loop = asyncio.get_running_loop()

        # Initialize agent client (OpenAI-compatible chat completions)
        self.agent_client = AgentClient(
            url=self.config.agent.url,
            api_key=self.config.api_key,
            model=self.config.agent.model,
            timeout=self.config.agent.timeout,
            system_prompt=self.config.agent.system_prompt,
            max_reply_bytes=self.config.meshtastic.max_message_length,
            max_retries=self.config.agent.max_retries,
            session_prefix=self.config.agent.session_prefix,
            shorten_retry=self.config.agent.shorten_retry,
        )
        await self.agent_client.start()

        if (
            self.config.meshtastic.direct_messages_only
            and not self.config.meshtastic.allowed_nodes
        ):
            logger.warning(
                "meshtastic.allowed_nodes is empty: any node that can send this "
                "bridge a direct message will reach the agent"
            )

        # Initialize radio with receive callback; the transport decides how the
        # node is reached (serial today, other methods slot in via TRANSPORTS)
        self.radio = MeshtasticRadio(
            transport=build_transport(self.config),
            hop_limit=self.config.meshtastic.hop_limit,
            on_receive=self._on_receive,
            on_packet=self._on_packet,
        )

        # Connect to radio
        if not await self.radio.connect():
            logger.error("Failed to connect to radio, exiting")
            sys.exit(1)

        # Initialize HTTP server
        self.http_server = BridgeHTTPServer(
            host=self.config.bridge.host,
            port=self.config.bridge.http_port,
            send_fn=self._send_to_radio,
            health_fn=self._get_health,
        )
        await self.http_server.start()

        logger.info("Meshtastic Bridge started successfully")

    async def stop(self) -> None:
        """Stop all components."""
        logger.info("Stopping Meshtastic Bridge...")

        if self.http_server:
            await self.http_server.stop()

        if self.radio:
            await self.radio.disconnect()

        if self.agent_client:
            await self.agent_client.stop()

        logger.info("Meshtastic Bridge stopped")

    def _on_packet(self, packet: dict) -> None:
        """Every received packet, any type: tracks whether the radio hears anything."""
        own_num = self.radio.my_node_num if self.radio else None
        if own_num is not None and packet.get("from") == own_num:
            # Our own transmission echoed back by the interface: the serial link
            # is alive, but it says nothing about what this radio can hear.
            self._own_packet_count += 1
            return

        self._last_packet_at = time.monotonic()
        self._packet_count += 1

    def _on_receive(self, packet: dict) -> None:
        """Callback when message received from LoRa."""
        from_id = packet.get("from_id")
        text = packet.get("text")
        channel = packet.get("channel", 0)
        to_id = packet.get("to_id")

        if not from_id or not text:
            return

        # Schedule the coroutine in the main event loop from this callback thread
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._forward_to_agent(from_id, text, channel, to_id),
                self._loop
            )

    def _log_rejection(self, reason: str, from_id: str, extra: str = "") -> None:
        """Log a dropped message at most once per node per minute (flood safe)."""
        key = normalize_node_id(from_id) or (from_id or "unknown")
        now = time.monotonic()
        last = self._rejected_logged.get(key)
        if last is not None and now - last < 60:
            return
        self._rejected_logged[key] = now
        detail = f" ({extra})" if extra else ""
        logger.info(f"Dropped {reason} from {from_id}{detail}")

    async def _forward_to_agent(
        self, from_id: str, text: str, channel: int, to_id: Optional[str] = None
    ) -> None:
        """
        Forward an inbound message to the agent and send its reply back over LoRa.

        Policy, in order: direct-messages-only, then the sender allowlist, then the
        automated-notification filter. Anything dropped is logged (rate limited per
        node). The loop itself is synchronous: one request, one reply, and a
        failure transmits nothing so a broken agent never spams the mesh.
        """
        if self.agent_client is None or self.radio is None:
            logger.error("Bridge not started, dropping inbound message")
            return

        # Channel broadcasts, and anything not addressed to this node, are not ours.
        # Direct messages are PKI-encrypted end to end, so this keeps the agent off
        # traffic every node on the channel can read.
        if self.config.meshtastic.direct_messages_only:
            own_id = normalize_node_id(self.radio.my_node_id)
            if not own_id:
                self._log_rejection(
                    "message (own node id unknown, fail closed)", from_id
                )
                return
            if is_broadcast_id(to_id) or normalize_node_id(to_id) != own_id:
                self._log_rejection(
                    "channel message", from_id, f"to={to_id or 'unknown'}"
                )
                return

        # Only known handsets reach the agent
        if not self.config.meshtastic.is_allowed(from_id):
            self._log_rejection(
                "message from a node not in allowed_nodes", from_id
            )
            return

        # Automated notifications (position shares and the like) are not for the agent
        if self.config.agent.is_ignored(text):
            logger.info(
                f"Ignoring automated notification from {from_id}: {text[:40]}..."
            )
            return

        reply = await self.agent_client.ask(text=text, from_id=from_id)

        if not reply:
            logger.error(
                f"Agent gave no reply for message from {from_id}, nothing sent"
            )
            return

        sent = await self.radio.send_text(
            text=reply,
            destination=from_id,
            channel=channel,
            want_ack=self.config.meshtastic.want_ack,
            ack_timeout=self.config.meshtastic.ack_timeout,
        )

        if not sent:
            logger.error(f"Failed to send reply to {from_id} over LoRa")

    async def _send_to_radio(
        self, destination_id: Optional[str], message: str, channel: int
    ) -> bool:
        """Send message to radio (called by HTTP server)."""
        if not self.radio or not self.radio.is_connected:
            logger.error("Radio not connected")
            return False

        return await self.radio.send_text(
            text=message,
            destination=destination_id,
            channel=channel,
            want_ack=self.config.meshtastic.want_ack,
            ack_timeout=self.config.meshtastic.ack_timeout,
        )

    async def _get_health(self) -> dict:
        """Get health status (called by HTTP server)."""
        agent_reachable = False
        if self.agent_client:
            try:
                agent_reachable = await self.agent_client.check_health()
            except Exception as e:
                logger.error(f"Health check failed: {e}")

        # A radio that hears nothing while the serial link is up is the signature
        # of a deaf receiver; surfacing the age of the last packet makes that
        # visible in seconds instead of after an hour of silence.
        last_inbound_seconds = None
        if self._last_packet_at is not None:
            last_inbound_seconds = int(time.monotonic() - self._last_packet_at)

        return {
            "serial": self.radio.is_connected if self.radio else False,
            "agent_reachable": agent_reachable,
            # Packets from OTHER nodes: the real "can this radio hear" signal
            "inbound_packets": self._packet_count,
            "last_inbound_seconds": last_inbound_seconds,
            # Our own transmissions, echoed by the interface: proves the serial
            # link and the radio's TX work, not that anything is being received
            "own_packets": self._own_packet_count,
        }

    async def run(self) -> None:
        """Run until shutdown signal."""
        await self.start()

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        await self.stop()

    def shutdown(self) -> None:
        """Signal shutdown."""
        self._shutdown_event.set()


async def main(config: Optional[Config] = None) -> None:
    """Run the bridge until a shutdown signal."""
    config = config or Config.load()

    # Setup logging before config validation so load/validate messages are visible
    logging.basicConfig(
        level=getattr(logging, config.bridge.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info(f"Loaded config: agent={config.agent.url} port={config.bridge.http_port}")

    config.validate()

    # Create bridge
    bridge = MeshtasticBridge(config)

    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        bridge.shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # Run bridge
    try:
        await bridge.run()
    except Exception as e:
        logger.error(f"Bridge error: {e}", exc_info=True)
        sys.exit(1)


async def check(config: Config, config_path: Optional[Path] = None) -> bool:
    """
    Verify the deployment without opening the radio (no reset side effects).

    Checks, in order: config validity, the configured transport and its
    prerequisites, the agent API key, agent reachability, and the inbound policy.
    Returns True when everything that must work does.
    """
    ok = True

    print(f"config file  : {config_path or 'standard location'}")
    print(f"bridge http  : http://{config.bridge.host}:{config.bridge.http_port}")

    try:
        config.validate()
        print("config       : OK")
    except ValueError as exc:
        ok = False
        print(f"config       : FAIL {exc}")

    try:
        transport = build_transport(config)
    except ValueError as exc:
        ok = False
        print(f"connection   : {config.meshtastic.connection} FAIL {exc}")
    else:
        print(f"connection   : {config.meshtastic.connection} ({transport.describe()})")
        ready, detail = transport.prerequisites_met()
        ok = ok and ready
        print(f"             : {'OK' if ready else 'FAIL'} {detail}")

    print(f"agent url    : {config.agent.url}")
    if config.api_key:
        print(f"             : OK {config.agent.api_key_env} is set")
    else:
        ok = False
        print(f"             : FAIL {config.agent.api_key_env} is not set in the environment")

    client = AgentClient(
        url=config.agent.url,
        api_key=config.api_key,
        model=config.agent.model,
        timeout=config.agent.timeout,
        system_prompt=config.agent.system_prompt,
        session_prefix=config.agent.session_prefix,
        shorten_retry=config.agent.shorten_retry,
    )
    reachable = False
    await client.start()
    try:
        reachable = await client.check_health()
    finally:
        await client.stop()
    ok = ok and reachable
    print(f"             : {'OK' if reachable else 'FAIL'} agent /health {'answered' if reachable else 'did not answer'}")

    allowed = config.meshtastic.allowed_nodes
    print(
        f"inbound      : direct messages only={config.meshtastic.direct_messages_only}, "
        f"allowed_nodes={len(allowed) if allowed else 'any'}, "
        f"replies capped at {config.meshtastic.max_message_length} bytes"
    )
    if config.meshtastic.direct_messages_only and not allowed:
        print("             : WARN no allowed_nodes set, any node that can DM this bridge reaches the agent")

    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


def cli(argv: Optional[list] = None) -> int:
    """Console entry point: ``meshtastic-bridge [--config PATH] [--check]``."""
    parser = argparse.ArgumentParser(
        prog="meshtastic-bridge",
        description="Bridge a Meshtastic LoRa node to an OpenAI-compatible agent API.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config file to use (default: MESHTASTIC_BRIDGE_CONFIG, /etc/meshtastic-bridge/config.yaml, ~/.config/meshtastic-bridge/config.yaml, ./config.yaml)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config, transport and agent reachability, then exit (never opens the radio)",
    )
    args = parser.parse_args(argv)

    try:
        config = Config.load(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        return 0 if asyncio.run(check(config, args.config)) else 1

    try:
        asyncio.run(main(config))
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
