"""
How the bridge reaches the node it serves.

One class per connection method. ``TRANSPORTS`` is the registry that
``build_transport`` resolves ``meshtastic.connection`` against, so a new method
(Bluetooth, TCP, a mock in tests) is a class plus one registry entry, and nothing
else in the bridge changes.

Contract for a transport:
  - ``from_config(config)``  builds it from the whole app config, reading the
                            block it needs (serial today, a ble block later)
  - ``describe()``          one line for logs and ``--check``
  - ``prerequisites_met()``  (ok, detail) with no side effects, safe to call
  - ``prepare()``           optional pre-open hook (serial's DTR/RTS wake pulse)
  - ``open()`` / ``close()`` the meshtastic interface object
"""

from __future__ import annotations

import abc
import logging
import os
import time
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


class Transport(abc.ABC):
    """A way to reach a Meshtastic node."""

    name: str = ""

    @classmethod
    @abc.abstractmethod
    def from_config(cls, config) -> "Transport":
        """Build this transport from the app config (bridge.config.Config)."""

    @abc.abstractmethod
    def describe(self) -> str:
        """What this transport talks to, for logs and ``--check``."""

    @abc.abstractmethod
    def prerequisites_met(self) -> Tuple[bool, str]:
        """(ok, detail). Must not open anything or cause side effects."""

    def prepare(self) -> None:
        """Runs just before ``open()``; no-op unless the transport needs one."""

    @abc.abstractmethod
    def open(self) -> Any:
        """Return an open meshtastic interface. Raises on failure."""

    @abc.abstractmethod
    def close(self, interface: Any) -> None:
        """Close an interface returned by ``open()``."""


class SerialTransport(Transport):
    """USB serial node: CP210x bridge or native USB-CDC."""

    name = "serial"

    # A node in light sleep only answers serial after the auto-reset pulse, and
    # the firmware needs a moment to bring the application (and radio) back up.
    WAKE_PULSE_SETTLE_S = 1.0

    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 921600):
        self.port = port
        self.baud = baud

    @classmethod
    def from_config(cls, config) -> "SerialTransport":
        return cls(
            port=config.serial.port,
            baud=config.serial.baud,
        )

    def describe(self) -> str:
        return f"serial {self.port} @ {self.baud} baud"

    def prerequisites_met(self) -> Tuple[bool, str]:
        if not os.path.exists(self.port):
            return False, f"{self.port} does not exist (node unplugged, or wrong serial.port)"
        if not os.access(self.port, os.R_OK | os.W_OK):
            return False, f"{self.port} is not readable/writable (user in the dialout group?)"
        return True, f"{self.port} present and writable"

    def prepare(self) -> None:
        """DTR/RTS auto-reset pulse: wakes a sleeping node, reboots an ESP32."""
        import serial

        pulse = serial.Serial(self.port, self.baud, timeout=0.5)
        pulse.setDTR(False)
        pulse.setRTS(True)
        time.sleep(0.15)
        pulse.setRTS(False)
        time.sleep(0.5)
        pulse.close()
        time.sleep(self.WAKE_PULSE_SETTLE_S)

    def open(self) -> Any:
        from meshtastic.serial_interface import SerialInterface

        return SerialInterface(self.port)

    def close(self, interface: Any) -> None:
        interface.close()


TRANSPORTS: Dict[str, type] = {
    SerialTransport.name: SerialTransport,
}


def build_transport(config) -> Transport:
    """Instantiate the transport named by ``meshtastic.connection``."""
    name = (getattr(config.meshtastic, "connection", "") or "").strip().lower()
    factory = TRANSPORTS.get(name)
    if factory is None:
        raise ValueError(
            f"unknown meshtastic.connection {name!r}; "
            f"registered transports: {', '.join(sorted(TRANSPORTS))}"
        )
    return factory.from_config(config)
