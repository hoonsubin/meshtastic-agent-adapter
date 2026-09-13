#!/usr/bin/env python3
"""
Deaf-receiver detector for a Meshtastic node (read-only diagnostic).

Usage:
    python3 mesh_sniffer.py [PORT] [SECONDS]     # defaults: /dev/ttyUSB0 90

Listens for EVERY packet type (not just text), printing signal quality, then
prints a verdict. On a mesh with other traffic a healthy node hears packets from
other nodes; a node that only ever hears its own transmissions has a broken
receive path (antenna or front-end), which no software change can fix.

Deliberately does NOT pulse DTR/RTS (that resets an ESP32 board). Only one
process can hold the serial port, so stop any other client first.

Requires the meshtastic package and pypubsub in the interpreter you run it with.
"""

import sys
import time

from meshtastic.serial_interface import SerialInterface
from pubsub import pub

port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 90

total = 0
own = 0
my_num = None


def on_receive(packet, interface):
    global total, own
    total += 1
    sender = packet.get("fromId")
    if my_num is not None and packet.get("from") == my_num:
        own += 1
    decoded = packet.get("decoded", {}) or {}
    print(
        f"[pkt {total:03d}] from={sender} port={decoded.get('portnum')} "
        f"snr={packet.get('rxSnr')} rssi={packet.get('rxRssi')} "
        f"text={(decoded.get('text') or '')[:40]!r}",
        flush=True,
    )


pub.subscribe(on_receive, "meshtastic.receive")
iface = SerialInterface(port)
my_num = getattr(getattr(iface, "myInfo", None), "my_node_num", None)
print(
    f"sniffing on {port} as !{my_num:08x} for {seconds}s (all packet types)"
    if my_num
    else f"sniffing on {port} for {seconds}s (all packet types)",
    flush=True,
)

try:
    time.sleep(seconds)
finally:
    iface.close()

others = total - own
print(f"\ntotal={total} own={own} from_others={others}", flush=True)

if others > 0:
    print(
        f"VERDICT: RX working, heard {others} packet(s) from other nodes in "
        f"{seconds}s",
        flush=True,
    )
elif total > 0:
    print(
        "VERDICT: RX DEAF SUSPECT. Only this node's own transmissions were seen. "
        "Compare with a neighbour's channel utilisation (10-40% on a busy mesh "
        "means the receiver is deaf): reseat or swap the antenna, then power "
        "cycle the board and re-run.",
        flush=True,
    )
else:
    print(
        "VERDICT: RX DEAF SUSPECT. Nothing at all arrived in "
        f"{seconds}s, not even this node's own telemetry. Check the antenna and "
        "power cycle the board.",
        flush=True,
    )
