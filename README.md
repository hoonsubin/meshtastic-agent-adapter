# Meshtastic Agent Adapter

Bidirectional LoRa messaging between a Meshtastic mesh and OpenAI-compatible endpoints, over a
single synchronous HTTP call. The bridge is agent-agnostic: anything that speaks
OpenAI-compatible chat completions on the LAN can be on the other end.

This implementation is tested on the Hermes Agent.

Now you can't escape AI even without internet!

```
LoRa handset/node
      |  (serial /dev/ttyUSB0, held by the bridge)
      v
bridge (Linux host connected to the radio)
      |  POST /v1/chat/completions   Authorization: Bearer <API_SERVER_KEY>
      |  X-Hermes-Session-Id: meshtastic-<node>        (per-node continuity)
      v
Hermes Agent API server  (port 8642 by default)
      |  choices[0].message.content
      v
bridge truncates to the LoRa byte budget and sends it back to the sender
```

One request, one reply. No webhook route, no HMAC handshake, no callback URL.

## Why this design

Three approaches were built or sketched during development; only the third is
shipped. The other two are kept here as documented dead ends.

| Approach | Verdict |
| --- | --- |
| Full platform adapter plugin + webhook route (`deliver: meshtastic`) | Abandoned. Three layers (plugin, webhook route with a prompt that curls the callback, dual HMAC) produced two competing delivery paths and an HMAC signature handshake that rejected every inbound message with 401. Removed from the Hermes config. |
| Webhook PoC, one-way | Abandoned. No reply path, so it cannot hold a conversation. |
| **API-server bridge (this repo)** | **Shipped.** The bridge owns the radio and the reply hop, so the whole loop is one function deep. Failure is loud and local: if the agent is unreachable the bridge logs the error and sends nothing. |

Trade-off accepted: every LoRa message is a plain API call, so there are no
slash commands. Per-node conversation continuity is kept by sending a stable
`X-Hermes-Session-Id` per mesh node, which the API server threads as a session.

## Setup

The bridge runs on the host that has the radio; the agent can be on that host or
on another one. Two values change between the two topologies (`AGENT_HOST` and
`AGENT_PORT` in `.env`); everything else is the same. The full walkthrough
with the values table is `docs/SETUP.md`.

### Preconditions

- **Agent host** is up, the API server is enabled (see `Agent side` below),
  and reachable from the radio host on `AGENT_HOST:AGENT_PORT`. The bridge will
  fail to start if it cannot reach `/v1/chat/completions` on first message.
- **Radio host** has Python 3.10+ (or Docker), the Meshtastic node on
  `/dev/ttyUSB0` (or `/dev/ttyACM0`), and the service account in `dialout`.
- **The radio and every handset** share a channel name, channel PSK, region and
  modem preset. Without that, the radio link does not exist; the bridge can do
  nothing.

### Two install paths

```bash
# Path A — run it as a command on the radio host (development / one-off)
python3 -m venv .venv && .venv/bin/pip install -e .
MESHTASTIC_AGENT_KEY=<api-server-key> AGENT_HOST=192.168.0.100 AGENT_PORT=8642 \
  .venv/bin/meshtastic-bridge

# Path B — installer + systemd (Debian hosts, intended for a fixed SBC)
sudo MESHTASTIC_AGENT_KEY=<api-server-key> AGENT_HOST=192.168.0.100 AGENT_PORT=8642 \
  bash deploy/install.sh
sudo systemctl restart meshtastic-bridge      # pick up the values just written
```

The installer seeds `/etc/meshtastic-bridge/.env` from `.env.example` on first
run, then writes the values you passed (`MESHTASTIC_AGENT_KEY` plus any
`AGENT_HOST` / `AGENT_PORT` / `AGENT_MODEL` / `SERIAL_PORT` exported in the
shell) on top. Re-runs preserve any value you already set in `.env`, unless
you re-export it on the command line — shell values win. Open the file with
`sudo nano /etc/meshtastic-bridge/.env` to confirm before the final restart
if you want.

Path A reads the variables straight from the shell, so a one-line invocation
is enough. Path B uses `.env` properly: the env file is owned by root,
mode 0600, and read by systemd via `EnvironmentFile=`.

### What the installer does, in order

1. Verifies `/dev/ttyUSB0`, Python ≥ 3.10, and `systemctl`.
2. Installs `python3-venv`.
3. Creates the `meshtastic` system user (group `dialout`) and
   `/var/log/meshtastic-bridge`.
4. Copies the code to `/opt/meshtastic-bridge` and installs deps in a venv.
5. Installs `/etc/meshtastic-bridge/config.yaml` **only if absent** (your edits
   are never overwritten).
6. Installs `/etc/meshtastic-bridge/.env` **only if absent**, copying from
   `.env.example`. Then, regardless of whether the file was just seeded or
   already existed, writes any `MESHTASTIC_AGENT_KEY` / `AGENT_HOST` /
   `AGENT_PORT` / `AGENT_MODEL` / `SERIAL_PORT` exported in the installer's
   shell on top — shell values win, existing file values are kept otherwise.
   Mode 0600, root-owned.
7. Installs the systemd unit with `EnvironmentFile=` pointing at `.env`.
8. Enables and restarts the service, polls `/health` for up to 30 s, then probes
   the agent's `/health`.

The API key is never stored in `config.yaml`; the config only names the
environment variable (`agent.api_key_env`). The service refuses to start when
that variable is missing rather than running degraded.

### Verify

```bash
.venv/bin/meshtastic-bridge --check --config config.yaml   # pre-flight, never opens the radio
# or, without the venv activated:
.venv/bin/python -m bridge.main --check --config config.yaml

bash scripts/verify_bridge.sh <radio-host>:8085            # health, optional probe send
sudo journalctl -u meshtastic-bridge -f                    # live logs
```

`--check` never opens the radio, so it is safe to run any time (opening the
serial port pulses DTR/RTS and reboots an ESP32 node). It reports: config
validity, the transport and whether its device is present, the agent URL and
whether `/health` answered, and the inbound policy. Exit code 0 means PASS.

### Config

`config.yaml` in the repo root is the single source of truth. Strings may
contain `${VAR}` or `${VAR:-default}` placeholders that are expanded from the
environment at load time. Names have to be UPPER_SNAKE_CASE, which keeps the
substitution away from the `{max_bytes}` and `{from_id}` placeholders in
`system_prompt`. The systemd unit reads `/etc/meshtastic-bridge/.env`
(root-owned, mode 0600), so that file is the natural place to set host-specific
values — `.env.example` in the repo is the template.

Two practical rules:

- An unset `${VAR}` (no default) stays as the literal `${VAR}` in the parsed
  config. Missing values fail loudly later (URL parse error, agent 404,
  validation message) instead of silently becoming `""`.
- `${VAR:-default}` (shell-style) substitutes `default` only when `VAR` is unset.
  A set-but-empty value is treated as set, so the default does not apply.

```yaml
serial:
  port: /dev/ttyUSB0
  baud: 921600
meshtastic:
  hop_limit: 3
  max_message_length: 200      # LoRa byte budget for replies
agent:
  url: http://${AGENT_HOST}:${AGENT_PORT}/v1/chat/completions
  model: ${AGENT_MODEL:-hermes-agent}
  api_key_env: MESHTASTIC_AGENT_KEY
  timeout: 60                  # a full agent turn can be slow; the handset waits
  max_retries: 1               # then stay silent rather than spam the mesh
  session_prefix: meshtastic   # -> X-Hermes-Session-Id per node
  system_prompt: |             # tells the agent it is on a radio link
    ...
bridge:
  host: 0.0.0.0
  http_port: 8085
```

### Agent side (Hermes)

The API server must be enabled and reachable from the bridge host. On the agent
host:

```bash
# ~/.hermes/.env
API_SERVER_KEY=<strong random key>
API_SERVER_HOST=0.0.0.0        # bind address; restrict with the firewall, not by binding to localhost
```

`GET /health` on the API server needs no auth; `/v1/chat/completions` needs the
Bearer key. Because that key grants full agent access on the agent host, do
**not** rely on binding alone for security: keep port 8642 firewalled to the
bridge host (and the tailnet if applicable). Binding to `127.0.0.1` would block
the bridge, which is on a different host in the radio-elsewhere topology.

## Operations

```bash
curl http://BRIDGE-HOST:8085/health
# {"status":"ok","uptime":42,"serial":true,"agent_reachable":true,
#  "inbound_packets":9,"last_inbound_seconds":12}

# inbound_packets counts packets from OTHER nodes (the real "can this radio hear"
# signal) and last_inbound_seconds ages the last of those; own_packets counts this
# node's own transmissions echoed by the interface, which proves the serial link
# and TX work but says nothing about receiving. The bridge logs the delivery side
# as a warning when a handset never confirms:
#   WARNING bridge.radio: No routing ACK from !68916e4c within 8s (packet ...);
#   the message may not have been received

curl -X POST http://BRIDGE-HOST:8085/send \
  -H 'Content-Type: application/json' \
  -d '{"destination_id":"!68916e4c","message":"agent-initiated","channel":0}'

sudo journalctl -u meshtastic-bridge -f     # logs
sudo bash deploy/revert.sh                  # uninstall (also removes the key file)
```

`/send` and `/callback` exist for agent-initiated messages. The reply path for
inbound LoRa messages does not use them: the bridge answers inline.

## Who can talk to the agent

Two gates, both on by default:

1. **Direct messages only** (`meshtastic.direct_messages_only: true`). The bridge
   answers only packets addressed to its own node id. Channel broadcasts are
   dropped and logged. This matters because Meshtastic secures the two cases
   differently: channel traffic is encrypted with the shared channel PSK, so every
   node holding that PSK can read it, while direct messages are encrypted to the
   recipient's public key (PKI, firmware 2.5+), so only the addressed node can.
   Answering DMs only means the agent never sees traffic the whole channel can.
2. **Sender allowlist** (`meshtastic.allowed_nodes`). Only these node ids reach
   the agent. An empty list allows any sender and logs a warning at startup.

Adding a handset is a copy and paste: an unknown sender produces one line per
minute,

```
INFO __main__: Dropped message from a node not in allowed_nodes from !abcdef12
```

so the id can be read straight out of the journal and added to `config.yaml`,
followed by a config copy to `/etc/meshtastic-bridge/config.yaml` and a restart.
Both ends still need the same channel (name and PSK) for the radio link to exist
at all; the gates decide who gets an agent turn and what the agent is allowed to
see.

## Portable skill set

`.agents/skills/meshtastic-bridge/` ships an agent skill with this repo, so any
Hermes instance can deploy and operate the bridge without rediscovering it:

```
.agents/skills/meshtastic-bridge/
├── SKILL.md                     # triggers, procedure, pitfalls, verification
├── references/
│   ├── deployment.md            # host prep, installer behaviour, config table, upgrades, revert
│   ├── hermes-api-server.md     # enabling/securing the API server on any instance
│   └── troubleshooting.md       # software vs radio faults, with evidence to collect
└── scripts/
    ├── verify_bridge.sh         # health + optional probe send, exits non-zero on failure
    ├── mesh_listen.py           # text listener, no reset
    └── mesh_sniffer.py          # all-packet sniffer with a deaf-receiver verdict
```

Install it on another Hermes instance either way:

```bash
# project-local (skills load while working in this checkout)
hermes skills trust /path/to/hermes-meshtastic-adapter

# or globally for that instance
cp -r .agents/skills/meshtastic-bridge ~/.hermes/skills/iot/
```

Pointing a *different* Hermes instance at this bridge is then three values: its
own `API_SERVER_KEY` + `API_SERVER_HOST`, `agent.url` in `config.yaml`, and the
matching key in `/etc/meshtastic-bridge/.env` on the radio host. Everything
is agent-agnostic HTTP, so a non-Hermes agent that speaks OpenAI-compatible chat
completions works too.

## Testing

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m bridge.main --check --config config.yaml   # deployment check, no radio
```

106 tests cover config/env handling, byte-safe truncation, session ids, reply
parsing, auth and retry behavior, delivery confirmation, the direct-message and
allowlist policy, the transport seam, the reply hop, the `--check` command and the
HTTP API.

Verified on real hardware with two nodes (sender on the workstation, relay plus
bridge on the SBC): prompt over LoRa, bridge round trip to the agent, reply back
over LoRa; a follow-up question was answered from the earlier context (session
continuity), and a 310-character answer arrived truncated to exactly 200 bytes.

## LoRa constraints

- Text budget: about 200 bytes after encryption (the bridge enforces this in bytes,
  not characters, so non-ASCII text cannot overflow it)
- The budget travels with every request: the system prompt states it and the user
  message repeats it, since a system-prompt-only limit was ignored in practice. A
  reply that still overshoots gets one rewrite request (tokens, not airtime) and
  only then a marked truncation
- Airtime: 1-3 seconds per message
- EU_868 duty cycle: 10% (360 s per rolling hour, per the region table), so a
  200-byte reply costs about 1.7 s of a shared, duty-cycle-limited budget. This is
  why the bridge answers with one packet and rewrites rather than chunking

## Project layout

```
hermes-meshtastic-adapter/
├── bridge/
│   ├── main.py          # orchestrator, reply hop, policy, --check CLI
│   ├── config.py        # YAML + env-secret config, validation
│   ├── transport.py     # how the node is reached (TRANSPORTS registry)
│   ├── radio.py         # MeshtasticRadio: pubsub receive, async send, ACKs
│   ├── http_server.py   # /send, /callback, /health for agent-initiated traffic
│   ├── http_client.py   # OpenAI-compatible client for inbound messages
│   └── text.py          # byte-safe truncation
├── docs/SETUP.md        # the guided setup page
├── scripts/             # verify_bridge.sh, mesh_listen.py, mesh_sniffer.py
├── deploy/              # install.sh, revert.sh, systemd unit
├── .agents/skills/meshtastic-bridge/   # portable agent skill set
├── tests/test_bridge.py
├── config.yaml          # source of truth (deployed to /etc/meshtastic-bridge/)
├── .env.example        # template for /etc/meshtastic-bridge/.env
└── PLANS.md             # phase status
```

## References

- [Hermes Agent docs](https://hermes-agent.nousresearch.com/docs)
- [Meshtastic Python API](https://meshtastic.org/docs/software/python/python-cli)
