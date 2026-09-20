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


class BLETransport(Transport):
    """
    BLE node: connects to a meshtastic node over Bluetooth Low Energy.

    Meshtastic firmware (NimBLE) advertises the meshtastic NUS service
    (6ba1b218-15a8-461f-9fa8-5dcae273eafd) only while no central is connected;
    the bridge holds the connection for its lifetime, so the node only advertises
    between bridge restarts (and during a brief scan window at startup).

    Pairing and trust happen at the OS level (bluez) once, before the bridge runs:

        bluetoothctl pair <addr>      # prompted for the fixed PIN/passkey
        bluetoothctl trust <addr>     # auto-reconnect after the bridge exits

    The meshtastic BLE interface always scans to find the device on connect, so
    the node must be advertising when the bridge starts - disconnect any prior
    BLE consumer (the CLI, a phone app) first. After the bridge is up, it holds
    the only BLE connection the node supports.

    The service user must be in the ``bluetooth`` group (deploy/install.sh does
    this) so the underlying bluez DBus calls are allowed.
    """

    name = "ble"

    def __init__(self, address: str):
        self.address = address

    @classmethod
    def from_config(cls, config) -> "BLETransport":
        address = getattr(config.ble, "address", "") or ""
        if not address:
            raise ValueError(
                "ble.address must be set when meshtastic.connection is 'ble'"
            )
        return cls(address=address)

    def describe(self) -> str:
        return f"BLE {self.address}"

    def prerequisites_met(self) -> Tuple[bool, str]:
        if not os.path.exists("/sys/class/bluetooth/hci0"):
            return False, "no Bluetooth adapter found at /sys/class/bluetooth/hci0"
        return True, f"Bluetooth adapter present, target BLE address {self.address}"

    def open(self) -> Any:
        from meshtastic.ble_interface import BLEInterface

        # BLEInterface.__init__ -> connect() -> find_device() -> scan() runs a
        # 10 s bleak discover() that only sees advertising devices. The bridge
        # process holds the BLE slot for its lifetime, so subsequent reconnects
        # inside one bridge run never scan (the call below is the only one).
        return BLEInterface(address=self.address)

    def close(self, interface: Any) -> None:
        interface.close()


class TCPTransport(Transport):
    """TCP node: connect over the node's TCP API (port 4403).

    Used when the node is reached through a BLE→TCP bridge (a BLE-only node
    exposed by a proxy) or a WiFi node. Unlike BLE (one client), a TCP server
    accepts many clients, so the bridge and a management UI can share the same
    node simultaneously.
    """

    name = "tcp"

    def __init__(self, host: str = "127.0.0.1", port: int = 4403):
        self.host = host
        self.port = port

    @classmethod
    def from_config(cls, config) -> "TCPTransport":
        host = getattr(config.tcp, "host", "") or ""
        port = getattr(config.tcp, "port", 4403)
        if not host:
            raise ValueError(
                "tcp.host must be set when meshtastic.connection is 'tcp'"
            )
        return cls(host=host, port=port)

    def describe(self) -> str:
        return f"TCP {self.host}:{self.port}"

    def prerequisites_met(self) -> Tuple[bool, str]:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect((self.host, self.port))
        except OSError as exc:
            return False, f"cannot reach TCP {self.host}:{self.port} ({exc})"
        finally:
            sock.close()
        return True, f"TCP {self.host}:{self.port} reachable"

    def open(self) -> Any:
        from meshtastic.tcp_interface import TCPInterface

        return TCPInterface(hostname=self.host, portNumber=self.port)

    def close(self, interface: Any) -> None:
        interface.close()


TRANSPORTS: Dict[str, type] = {
    SerialTransport.name: SerialTransport,
    BLETransport.name: BLETransport,
    TCPTransport.name: TCPTransport,
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
