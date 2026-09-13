# Troubleshooting reference

Goal: decide in minutes whether a broken LoRa agent link is software or radio,
and collect the evidence that settles it. Work top to bottom; each step either
clears a layer or names the culprit.

## 1. Read the one endpoint that sees everything

```
terminal(command="curl -s http://<radio-host>:8085/health")
```

| Field | Healthy | What a bad value means |
|---|---|---|
| `serial` | `true` | `false`: the service cannot talk to the node (port busy, unplugged, wrong `serial.port`) |
| `agent_reachable` | `true` | `false`: the Hermes API server is not answering `/health` from the radio host (wrong `agent.url`, firewall, API server off) |
| `inbound_packets` | climbing when a peer transmits | stuck at 0 with `own_packets` climbing: the radio hears nobody (deaf, or alone on the channel) |
| `own_packets` | climbing (this node's own beacons) | stuck: the serial link or the radio's TX is in trouble |
| `last_inbound_seconds` | small | `null` (never heard anything) or growing: a deaf receiver |

`inbound_packets` counts packets from OTHER nodes; `own_packets` counts this
node's own transmissions echoed by the interface. A radio with `serial: true`, a
climbing `own_packets` and a frozen `inbound_packets` is either deaf or alone on
its channel (a bridge with no handset plugged in looks exactly like this).

## 2. Is the radio deaf, or is the other end not transmitting?

Take the radio host first, then any suspect handset:

```
terminal(command="python3 scripts/mesh_sniffer.py /dev/ttyACM0 90")
```

The tool prints every packet with SNR/RSSI and then a verdict. Interpretation:

- `packets from others > 0`: the receiver hears the mesh; look elsewhere
- only its own telemetry (or nothing) while neighbouring nodes report 10-40%
  channel utilisation: **that node's receive path is broken**. Ask for an antenna
  reseat and a power cycle before touching software.
- a genuine RF fault survives reboots and shows no SNR variance, because nothing
  arrives to measure

Corroborate with the node's own view when you can reach it over serial:

```
terminal(command="meshtastic --port /dev/ttyACM0 --info | grep -E 'channelUtilization|airUtilTx'")
```

Run the tests in sequence, never two at once: one process per serial port, or the
second one dies with `Could not exclusively lock port`.

## 3. "I sent a reply and the handset never got it"

Look for the ACK line first:

```
terminal(command="ssh <user>@<radio-host> 'sudo journalctl -u meshtastic-bridge --since -15min | grep -E \"Sent message|No routing ACK|Delivery NAK\"'")
```

- `Sent message` then nothing: either the ACK arrived (confirmed) or
  `want_ack` is off. With `want_ack: true` a confirmed unicast logs nothing.
- `No routing ACK from <node> within Ns`: the handset never acknowledged. Treat it
  as "probably not received" and run step 2 on that handset.
- `Delivery NAK ... TIMEOUT` (or another reason): the mesh itself reported failure.

Remember the older limitation: without `want_ack`, a cheerful `Sent message` was
logged even for packets nobody received, which is exactly how a lost reply hides.

## 4. The agent answers but the content is wrong

- Replies cut off with a trailing `...`: the byte budget working as designed
  (`meshtastic.max_message_length`, default 200 bytes). Before cutting, the bridge
  asks the agent once to rewrite within the budget (`agent.shorten_retry`), so a
  truncation means even the rewrite overshot. The log says which happened:
  `Agent shortened its reply from N to M chars` versus `Reply truncated ... (still
  over budget after a shorten attempt)`. Raise the budget only if the handsets
  accept more, and remember the airtime cost: 200 bytes is about 1.7 s of
  duty-cycle-limited airtime per reply.
- Replies look like markdown, emoji or long lists: tighten `agent.system_prompt`.
  The limit is a request, not a guarantee; the byte truncation is the hard stop.
- The agent does not know who wrote: the prompt interpolates `{from_id}`.
- Memory feels wrong or stale: sessions are per node
  (`X-Hermes-Session-Id: <session_prefix>-<node>`), so one handset may carry my
  earlier test traffic. Change `agent.session_prefix` to start that node fresh.

## 5. Nothing reaches the agent at all

```
terminal(command="ssh <user>@<radio-host> 'sudo journalctl -u meshtastic-bridge --since -15min | grep -E \"Received from|Ignoring|Agent replied|ERROR\"'")
```

- `Received from` appears, `Agent replied` does not: the API call failed; check
  the ERROR line (`401` means the key in `.env` differs from the one on the
  Hermes side, `timeout` means the agent turn exceeds `agent.timeout`).
- `Dropped channel message ... (to=^all)`: the sender used a channel broadcast.
  Direct messages only is the default, so the sender has to open a direct chat
  with the relay's node instead of talking on the channel.
- `Dropped message from a node not in allowed_nodes from !xxxxxxxx`: add that id to
  `meshtastic.allowed_nodes`, copy the config to
  `/etc/meshtastic-bridge/config.yaml` and restart the service. The log is rate
  limited to one line per sender per minute, which is enough to discover a new
  handset's id.
- `Ignoring automated notification`: the message matched
  `agent.ignore_patterns`, by design. Widen or trim that list as needed.
- `Received from` never appears: nothing arrived over the air; go to step 2, and
  confirm both ends share channel name AND PSK:
  `meshtastic --port <port> --info | grep -A3 Channels:`.

## 6. Traps that cost real time

- **The wake pulse resets the node.** Every bridge restart reboots the relay it
  is attached to (the connect sequence pulses DTR/RTS), and so does any CLI
  wrapper that pulses. `uptimeSeconds` therefore resets on every restart and is
  useless as a crash indicator. Do not pulse a board that answers serial by
  itself, and allow ~10s after a reset before judging a send.
- **A reboot is not a fix for a deaf receiver unless it is a power cycle.**
  A firmware reset via USB re-initialises the app; only a real power cycle clears
  some front-end states. When diagnosing, ask for the power cycle explicitly.
- **Config drift.** The repo `config.yaml` is the source of truth;
  `/etc/meshtastic-bridge/config.yaml` is a copy that the installer preserves.
  Copy the repo file across when it changes.
- **Two nodes, same channel name, different PSK, silence.** Verify PSK, region
  (3 = EU_868) and modem preset (0 = LONG_FAST) on both ends; a mismatch in any of
  them produces the same nothing.
- **Expect seconds, not milliseconds.** A full agent turn over the radio path
  measured 4 to 15 seconds end to end. A sender that "gave up" sooner is not
  evidence of a fault.

## 7. What the bridge deliberately does not do

- No store-and-forward: if the recipient is off or deaf, the packet is gone.
  Delivery confirmation only covers "this node acknowledged", not "the human saw
  it".
- No per-node queue: two messages in flight at once can answer out of order.
- No rebroadcast dedupe: if duplicates ever show up in the field, add a packet-id
  cache rather than guessing in advance.
- No sender allowlist or rate limit yet: anything that can reach the channel can
  drive the agent. Treat the channel PSK as the security boundary, and the bridge
  HTTP API as LAN-internal.
