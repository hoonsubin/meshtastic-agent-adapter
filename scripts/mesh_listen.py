#!/usr/bin/env python3
"""
Listen for LoRa text messages on a Meshtastic node (read-only diagnostic).

Usage:
    python3 mesh_listen.py [PORT] [SECONDS]      # defaults: /dev/ttyUSB0 60

Prints one line per inbound text message. Deliberately does NOT pulse DTR/RTS:
that wake sequence resets an ESP32 board, and a reset node's radio is still
coming up for several seconds afterwards. Only one process can hold the serial
port, so stop any other client first.

Requires the meshtastic package and pypubsub in the interpreter you run it with.
"""

import sys
import time

from meshtastic.serial_interface import SerialInterface
from pubsub import pub

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 60

count = 0


def on_receive(packet, interface):
    global count
    decoded = packet.get("decoded", {}) or {}
    if decoded.get("portnum") != "TEXT_MESSAGE_APP":
        return
    count += 1
    text = (decoded.get("text") or "").strip()
    print(
        f"[rx #{count}] from={packet.get('fromId')} to={packet.get('toId')} "
        f"channel={packet.get('channel')} text={text!r}",
        flush=True,
    )


pub.subscribe(on_receive, "meshtastic.receive")
iface = SerialInterface(port)
my_num = getattr(getattr(iface, "myInfo", None), "my_node_num", None)
print(f"listening on {port} as !{my_num:08x} for {seconds}s" if my_num
      else f"listening on {port} for {seconds}s", flush=True)

try:
    time.sleep(seconds)
finally:
    iface.close()

print(f"done: {count} text message(s) in {seconds}s", flush=True)
