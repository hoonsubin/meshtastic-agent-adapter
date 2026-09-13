---
name: meshtastic-bridge
description: Bridge LoRa mesh messages to any Hermes instance.
version: 0.1.0
author: Hoon Kim
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Meshtastic, LoRa, Radio, Bridge, API Server]
---

# Meshtastic Bridge Skill

Give any Hermes instance a radio channel: a Meshtastic LoRa node answers inbound
mesh messages through the Hermes API server, and agent-initiated sends go out
through the bridge HTTP API. This skill covers deploying that bridge on a radio
host and pointing it at a Hermes instance, plus verifying it and diagnosing the
failures that look like software but are not.

## When to Use

- The user wants Hermes reachable over Meshtastic/LoRa, or wants mesh messages to
  reach an agent
- An existing bridge needs to be pointed at a different Hermes instance
- A LoRa link "stopped working" and needs to be diagnosed (see references)
- Don't use for: configuring node radios themselves (channels, PSK, region,
  power) beyond what deployment needs. That is node operations, not bridging.

## Prerequisites

On the radio host (Linux with systemd, the Meshtastic node on USB serial):

- A Meshtastic node reachable as `/dev/ttyUSB0` (CP210x) or `/dev/ttyACM0`
  (native USB), listed in config as `serial.port`
- Python 3.10+, `curl`, and root for the installer
- The node on the same channel (name AND PSK) as the handsets that will talk to
  the agent. Two nodes with the same channel name but different PSK never talk.

On the Hermes host (can be a different machine on the same LAN):

- API server enabled: `API_SERVER_KEY` (a strong key) and `API_SERVER_HOST`
  (see `references/hermes-api-server.md`)
- The bridge reaches it at `http://<hermes-host>:8642/v1/chat/completions`

Secrets: the agent API key lives in an env file on the radio host, never in
`config.yaml`. `agent.api_key_env` names the variable.

Handsets must send *direct messages to the bridge's node* (not channel broadcasts)
and their node ids must be listed in `meshtastic.allowed_nodes`. That is the
default posture: channel traffic is readable by every node holding the channel PSK,
while direct messages are PKI-encrypted to the recipient.

## How to Run

Install it as a command on the radio host (Python 3.10+), then run it:

```
terminal(command="cd <repo> && python3 -m venv .venv && .venv/bin/pip install -e .")
terminal(command="cd <repo> && MESHTASTIC_AGENT_KEY=<api-server-key> AGENT_HOST=<host> AGENT_PORT=8642 .venv/bin/meshtastic-bridge")
```

Debian hosts can use the installer plus systemd instead
(`sudo MESHTASTIC_AGENT_KEY=<key> AGENT_HOST=<host> AGENT_PORT=8642 bash deploy/install.sh`),
which writes `/opt/meshtastic-bridge`, `/etc/meshtastic-bridge/config.yaml`, the
mode-0600 env file at `/etc/meshtastic-bridge/.env` and the unit. Then verify
from anywhere on the LAN:

```
terminal(command="meshtastic-bridge --check")                          # safe: never opens the radio
terminal(command="bash scripts/verify_bridge.sh <radio-host>:8085")    # health, plus a probe send
```

## Quick Reference

| Purpose | Command |
|---|---|
| Health (bridge + agent + RX liveness) | `curl -s http://<radio-host>:8085/health` |
| Agent-initiated send | `curl -s -X POST http://<radio-host>:8085/send -H 'Content-Type: application/json' -d '{"destination_id":"!68916e4c","message":"hi","channel":0}'` |
| Logs | `sudo journalctl -u meshtastic-bridge -f` |
| Restart | `sudo systemctl restart meshtastic-bridge` |
| Uninstall | `sudo bash deploy/revert.sh` |
| Listen for text (no reset) | `python3 scripts/mesh_listen.py /dev/ttyACM0 60` |
| Deaf-receiver check | `python3 scripts/mesh_sniffer.py /dev/ttyACM0 90` |
| Who was rejected | `ssh <radio-host> "sudo journalctl -u meshtastic-bridge | grep Dropped"` |
| Loop check from the radio host | `bash scripts/verify_bridge.sh` |

## Procedure

1. Confirm the radio host really has the hardware: the serial device exists and
   nothing else holds it (`fuser /dev/ttyUSB0`). Installing on a host without the
   node wastes a revert cycle.
   Done when `ls -l <serial.port>` shows the device.
2. Read `<repo>/docs/SETUP.md` and deploy: either `pip install -e .` plus the
   `meshtastic-bridge` command, or the Debian installer which writes
   `/opt/meshtastic-bridge`, the config, the mode-0600 env file and the unit, then
   polls `/health` for up to 30s.
   Done when `systemctl is-active meshtastic-bridge` says active and `is-enabled`
   says enabled.
3. Point `agent.url` at the Hermes instance (repo `config.yaml` is the single
   source of truth) and copy that config to `/etc/meshtastic-bridge/config.yaml`
   before running the installer, which never overwrites an existing config.
   Done when `curl -s <agent-base>/health` answers 200 from the radio host.
4. Verify the health payload reports `serial: true`, `agent_reachable: true` and
   a small `last_inbound_seconds`. A null or ever-growing
   `last_inbound_seconds` with `serial: true` means the radio hears nothing.
   Done when all three fields look right.
5. Prove the loop with a real radio: send a message from a handset or from a
   second node on the same channel and watch the bridge log
   `Received from <node>` then `Agent replied` then `Sent message`.
   Done when the sender receives the answer.
6. Register the skill on the Hermes instance (optional, for other machines):
   `terminal(command="hermes skills trust <path-to-this-repo>")`, or copy
   `.hermes/skills/meshtastic-bridge` into `~/.hermes/skills/iot/` for a global
   install.
   Done when `hermes skills list` shows `meshtastic-bridge`.

## Pitfalls

- **A handset that gets no reply is usually not DMing the relay, or is not
  allowlisted.** The bridge answers direct messages only
  (`meshtastic.direct_messages_only`) from nodes in `meshtastic.allowed_nodes`.
  Both rejections are logged (once per minute per sender, with the id), so read
  `journalctl` and copy the id into the config rather than guessing.
- **"Sent message" does not mean delivered.** `sendText` returning only means the
  packet reached the radio. Unicast replies now request an ACK
  (`meshtastic.want_ack`) and log `No routing ACK from <node> within Ns` when the
  handset never confirms; treat that line as "the message probably did not
  arrive" and check the handset's receiver.
- **A silent bridge is usually a deaf receiver, not the software.** The symptom is
  `last_inbound_seconds` growing with `serial: true` and zero
  `Received from` lines. Run `scripts/mesh_sniffer.py` on the suspect node: if it
  sees only its own telemetry while neighbours report 10-40% channel utilisation,
  its receive path (antenna/front-end) is broken. Ask for an antenna reseat and a
  power cycle before touching any code.
- **A DTR/RTS wake pulse resets an ESP32 node.** Restarting the bridge reboots the
  relay it is attached to, and so does any CLI wrapper that pulses. Uptime is
  therefore useless as a crash indicator, and a send issued within ~10s of a
  reset can be dropped while the radio comes up. Do not pulse a node that answers
  serial on its own.
- **Two of your own probes cannot share one serial port.** A second listener dies
  with `Could not exclusively lock port`. Check `fuser <port>` first.
- **Config drift is silent.** The repo `config.yaml` is the source of truth;
  `/etc/meshtastic-bridge/config.yaml` is a copy, and the installer preserves it.
  Copy the repo file over it when the repo changes, or you will debug the wrong
  file. `MESHTASTIC_BRIDGE_CONFIG` tells the loader which file to read.
- **Automated traffic is not a conversation.** Position shares and app notices
  match `agent.ignore_patterns` and are dropped before the agent call. Widen that
  list rather than paying for agent turns on machine noise.
- **The 200 byte budget is bytes, not characters, and truncation is marked.**
  Replies are cut at a word boundary with a trailing `...`; multi-byte text is
  never split mid-character.
- **Every message is a fresh HTTP request, not a chat session.** Continuity comes
  from `X-Hermes-Session-Id: <session_prefix>-<node id>`, one conversation per
  node. Change `agent.session_prefix` to start that node over.

## Verification

- `curl -s http://<radio-host>:8085/health` reports `serial: true`,
  `agent_reachable: true`, a growing `inbound_packets` and a small
  `last_inbound_seconds`
- A real radio round trip lands: `Received from` -> `Agent replied` ->
  `Sent message` in `journalctl -u meshtastic-bridge`, with no
  `No routing ACK` warning for a healthy handset
- Unit tests pass in the checkout: `python3 -m venv .venv && .venv/bin/pip install
  -r requirements.txt pytest pytest-asyncio && .venv/bin/python -m pytest tests/ -q`

## References

- `<repo>/docs/SETUP.md` — the guided setup page: what to fill, same-host versus
  radio-elsewhere, the three run options, and verification
- `references/hermes-api-server.md` — enabling and securing the API server on any
  Hermes instance, endpoints, auth, session continuity
- `references/troubleshooting.md` — telling software faults from radio faults,
  with the evidence to collect first
- `scripts/verify_bridge.sh`, `scripts/mesh_listen.py`, `scripts/mesh_sniffer.py`
