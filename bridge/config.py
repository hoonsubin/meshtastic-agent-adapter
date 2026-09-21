"""
Configuration loader for Meshtastic Bridge

Loads YAML config with defaults and validation.

Secrets are never stored in the config file: the agent API key is read from the
environment variable named by `agent.api_key_env` (see deploy/install.sh, which
installs an env file read by the systemd unit).

Any string in the config may contain ${VAR_NAME} placeholders that are
expanded from os.environ before YAML parsing. This lets the same config file
travel across hosts without rewriting host-specific values (the agent URL,
the agent model name, etc.) — define them once in /etc/meshtastic-bridge/.env
next to MESHTASTIC_AGENT_KEY. The name has to be UPPER_SNAKE_CASE, which keeps
the substitution away from the {max_bytes} / {from_id} .format() placeholders
in agent.system_prompt.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .environment import expand_env_vars
from .transport import TRANSPORTS

logger = logging.getLogger(__name__)

# Config file the systemd unit points at, if any
CONFIG_ENV_VAR = "MESHTASTIC_BRIDGE_CONFIG"

# ${VAR} / ${VAR:-default} expansion is defined in bridge.environment (stdlib-only)
# so deploy/install.sh can reuse it without importing this module's yaml dependency.
# ``expand_env_vars`` is imported above and re-exported here for existing callers.


def expand_env_vars_in_obj(obj: Any) -> Any:
    """
    Recursively walk a parsed YAML structure and expand ${VAR} in every string.

    Lists and dicts are walked in place; scalar strings are expanded; other
    scalars are returned unchanged.
    """
    if isinstance(obj, dict):
        return {k: expand_env_vars_in_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_vars_in_obj(item) for item in obj]
    if isinstance(obj, str):
        return expand_env_vars(obj)
    return obj


# The Meshtastic library reports a broadcast destination as this string
# (BROADCAST_ADDR); the wire value is 0xFFFFFFFF.
BROADCAST_IDS = frozenset({"^all", "!ffffffff", "4294967295", "-1"})

DEFAULT_SYSTEM_PROMPT = (
    "You are answering a message that arrived over a LoRa mesh radio link. "
    "Reply in plain text only: no markdown, no code blocks, no lists and no emoji. "
    "Keep the whole reply under {max_bytes} bytes. If the full answer does not fit, "
    "give the short answer and point to where the rest lives (for example a URL), "
    "instead of trimming a long answer. Answer directly, with no preamble. "
    "The message came from mesh node {from_id}."
)


def normalize_node_id(value) -> str:
    """
    Normalize a node id for comparison: lowercase, single leading '!'.

    Accepts '!68916e4c', '68916E4C' or the numeric 1754361420 and returns
    '!68916e4c'. Returns '' for anything unusable.
    """
    if value is None:
        return ""
    if isinstance(value, int):
        return f"!{value:08x}"
    text = str(value).strip().lower()
    if not text:
        return ""
    if text.startswith("!"):
        text = text[1:]
    if not text:
        return ""
    # Numeric forms (decimal node numbers) become the hex form
    if text.isdigit() and len(text) > 8:
        try:
            return f"!{int(text):08x}"
        except ValueError:
            return ""
    return f"!{text}"


def is_broadcast_id(value) -> bool:
    """True when the destination is a broadcast rather than a single node."""
    if value is None:
        return False
    return str(value).strip().lower() in BROADCAST_IDS


@dataclass
class SerialConfig:
    port: str = "/dev/ttyUSB0"
    baud: int = 921600


@dataclass
class BLEConfig:
    """Settings for the BLE transport (ThinkNode M6 / Heltec / etc.)."""
    # MAC address of the meshtastic node (uppercase, colon-separated).
    # The node must already be paired+trusted at the OS level via bluetoothctl;
    # the transport does not pair on its own (see bridge/transport.py).
    address: str = ""


@dataclass
class TCPConfig:
    """Settings for the TCP transport (a BLE→TCP bridge, or a WiFi node)."""
    host: str = "127.0.0.1"
    port: int = 4403


@dataclass
class MeshtasticConfig:
    # Registered in bridge.transport.TRANSPORTS: how the node is reached
    connection: str = "serial"
    hop_limit: int = 3
    max_message_length: int = 200
    # Ask the mesh for a delivery ACK on unicast replies and log when none arrives
    want_ack: bool = True
    ack_timeout: float = 8.0
    # Only answer messages addressed directly to this bridge's node; channel
    # broadcasts are ignored. Direct messages are PKI-encrypted end to end, so
    # this also keeps the agent off shared-channel traffic.
    direct_messages_only: bool = True
    # Node ids allowed to reach the agent (direct messages only). Empty means any
    # node that can send this bridge a direct message gets an agent turn.
    allowed_nodes: list = field(default_factory=list)

    def is_allowed(self, from_id) -> bool:
        """True when the sender may reach the agent (empty list allows everyone)."""
        if not self.allowed_nodes:
            return True
        allowed = {normalize_node_id(n) for n in self.allowed_nodes}
        return normalize_node_id(from_id) in allowed


@dataclass
class AgentConfig:
    # OpenAI-compatible chat completions endpoint (Hermes API server)
    url: str = "http://127.0.0.1:8642/v1/chat/completions"
    model: str = "hermes-agent"
    # Name of the environment variable holding the agent API key
    api_key_env: str = "MESHTASTIC_AGENT_KEY"
    timeout: int = 60
    max_retries: int = 1
    # Prefix for the per-node agent session (X-Hermes-Session-Id)
    session_prefix: str = "meshtastic"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # When a reply overshoots the byte budget, ask the agent to rewrite it once
    # before truncating: an extra API call costs no radio airtime.
    shorten_retry: bool = True
    # Substrings (case-insensitive) that mark automated traffic we must not spend
    # an agent turn and a LoRa reply on
    ignore_patterns: list = field(
        default_factory=lambda: ["📍", "has shared their position"]
    )

    def is_ignored(self, text: str) -> bool:
        """True when the message matches an ignore pattern (case-insensitive)."""
        lowered = (text or "").lower()
        return any(p.lower() in lowered for p in self.ignore_patterns if p)


@dataclass
class BridgeConfig:
    host: str = "0.0.0.0"
    http_port: int = 8085
    log_level: str = "INFO"


@dataclass
class Config:
    serial: SerialConfig = field(default_factory=SerialConfig)
    ble: BLEConfig = field(default_factory=BLEConfig)
    tcp: TCPConfig = field(default_factory=TCPConfig)
    meshtastic: MeshtasticConfig = field(default_factory=MeshtasticConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)

    @property
    def api_key(self) -> str:
        """The agent API key, read from the environment (never from config)."""
        return os.environ.get(self.agent.api_key_env, "")

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        """Load config from YAML file, falling back to defaults."""
        if path is None:
            # The unit-provided path wins, then standard locations
            env_path = os.environ.get(CONFIG_ENV_VAR)
            search_paths = []
            if env_path:
                search_paths.append(Path(env_path))
            search_paths += [
                Path("/etc/meshtastic-bridge/config.yaml"),
                Path.home() / ".config" / "meshtastic-bridge" / "config.yaml",
                Path("config.yaml"),
            ]
            for p in search_paths:
                if p.exists():
                    path = p
                    break

        if path is None or not path.exists():
            logger.warning("No config file found, using defaults")
            return cls()

        logger.info(f"Loading config from {path}")

        with open(path) as f:
            # Expand ${VAR_NAME} placeholders before parsing so they can fill
            # any string field (url, system_prompt, ignore_patterns, ...).
            # The regex only matches UPPER_SNAKE_CASE names, so the {max_bytes}
            # and {from_id} .format() placeholders in system_prompt stay literal.
            raw = expand_env_vars(f.read())
        data = yaml.safe_load(raw) or {}

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Build Config from dictionary."""
        return cls(
            serial=SerialConfig(**data.get("serial", {})),
            ble=BLEConfig(**data.get("ble", {})),
            tcp=TCPConfig(**data.get("tcp", {})),
            meshtastic=MeshtasticConfig(**data.get("meshtastic", {})),
            agent=AgentConfig(**data.get("agent", {})),
            bridge=BridgeConfig(**data.get("bridge", {})),
        )

    def validate(self) -> None:
        """Validate config values. Raises ValueError on anything unusable."""
        if self.meshtastic.connection == "serial" and not self.serial.port:
            raise ValueError("serial.port is required when meshtastic.connection is 'serial'")
        if self.meshtastic.connection == "serial" and self.serial.baud <= 0:
            raise ValueError("serial.baud must be positive")
        if self.meshtastic.connection == "ble" and not self.ble.address:
            raise ValueError(
                "ble.address is required when meshtastic.connection is 'ble'"
            )
        if self.meshtastic.connection == "tcp" and not self.tcp.host:
            raise ValueError("tcp.host is required when meshtastic.connection is 'tcp'")
        if self.meshtastic.connection not in TRANSPORTS:
            raise ValueError(
                f"meshtastic.connection {self.meshtastic.connection!r} is not registered; "
                f"available: {', '.join(sorted(TRANSPORTS))}"
            )
        if self.meshtastic.hop_limit < 1 or self.meshtastic.hop_limit > 7:
            raise ValueError("meshtastic.hop_limit must be 1-7")
        if self.meshtastic.max_message_length < 1:
            raise ValueError("meshtastic.max_message_length must be positive")
        if self.meshtastic.ack_timeout < 0:
            raise ValueError("meshtastic.ack_timeout must be >= 0")
        if not isinstance(self.meshtastic.allowed_nodes, list):
            raise ValueError("meshtastic.allowed_nodes must be a list of node ids")
        if not self.agent.url:
            raise ValueError("agent.url is required")
        if self.agent.timeout < 1:
            raise ValueError("agent.timeout must be positive")
        if self.agent.max_retries < 0:
            raise ValueError("agent.max_retries must be >= 0")
        if not self.agent.api_key_env:
            raise ValueError("agent.api_key_env is required")
        if not self.api_key:
            raise ValueError(
                f"agent.api_key_env={self.agent.api_key_env!r} is not set in the "
                "environment. Put the agent API key in the bridge env file "
                "(see deploy/install.sh) and restart the service."
            )
        if self.bridge.http_port < 1 or self.bridge.http_port > 65535:
            raise ValueError("bridge.http_port must be 1-65535")
