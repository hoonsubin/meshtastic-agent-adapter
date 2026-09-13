# Setup

A guided setup for the bridge. Read this top to bottom on a fresh host, then
follow Path A (run as a command) or Path B (installer + systemd).

> **If you are an autonomous agent** (Hermes / Miki / Claude / etc.), prefer
> the agent skill shipped alongside this doc at
> `.agents/skills/meshtastic-bridge/SKILL.md`. It has the same procedure
> but in copy-pasteable form with explicit "Done when" checks at each step,
> and links into the troubleshooting flow at
> `.agents/skills/meshtastic-bridge/references/troubleshooting.md`.

Two hosts can be involved and only two values change between them:

- **radio host** — the machine the Meshtastic node is attached to. Today that
  is USB serial; the connection method is a pluggable transport
  (`bridge/transport.py`).
- **agent host** — a Hermes instance with its API server enabled. If it is the
  same machine as the radio host, the radio host *is* the agent host.

The bridge runs on the radio host and talks HTTP to the agent host.

## 1. Preconditions

Make sure these are true before installing anything:

1. **Agent host** is up. The Hermes API server is enabled (see
   `docs/SETUP.md §Agent side` in this repo, or
   `.agents/skills/meshtastic-bridge/references/hermes-api-server.md`). The
   bridge host can reach `AGENT_HOST:AGENT_PORT` and get a `200` from
   `/health`. Without that, the bridge starts but every LoRa message fails.

   ```bash
   curl -s http://AGENT-HOST:8642/health
   # {"status":"ok", ...}
   ```

2. **Radio host** has Python 3.10+ (or Docker), the Meshtastic node on
   `/dev/ttyUSB0` (or `/dev/ttyACM0`), and the service account in `dialout`
   (Path B's installer adds it for you). Confirm the device shows up:

   ```bash
   ls -la /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
   ```

3. **The radio and every handset** share a channel name, channel PSK, region
   and modem preset. Without that the radio link does not exist; the bridge
   can do nothing. Verify on the node's BLE screen or via the Meshtastic CLI:

   ```bash
   meshtastic --info
   ```

4. **One shared secret**: the agent API key. The bridge presents it as a Bearer
   token; that is the whole handshake. Pick it up from the agent host's
   `~/.hermes/.env` (`API_SERVER_KEY`).

## 2. Decide your topology

| Topology | `AGENT_HOST` in `.env` |
|---|---|
| Same host (radio and agent on one machine) | `127.0.0.1` |
| Radio elsewhere (most common — SBC at the radio, agent on a server) | the agent host's LAN address |

`AGENT_PORT` is `8642` for a default Hermes install. The only other env var the
shipped config references is `AGENT_MODEL` (already defaulted via
`${AGENT_MODEL:-hermes-agent}`), so you can skip it.

## 3. Values reference

Config lives in `config.yaml` (the repo copy is the source of truth). Rows
marked *topology* are the ones that change between "same host" and "radio
elsewhere"; everything else can stay at defaults.

| Key | Meaning |
|---|---|
| `meshtastic.connection` | Registered transport: `serial` today (`bridge/transport.py::TRANSPORTS`) |
| `serial.port`, `serial.baud` | Serial device and baud (921600 for Meshtastic). `port` may be `${SERIAL_PORT:-/dev/ttyUSB0}` when the bridge runs in a container or the tty name changes per boot |
| `agent.url` | **topology**: filled in by `${AGENT_HOST}` and `${AGENT_PORT}` from `.env` |
| `agent.api_key_env` | **topology**: the env var name holding the shared secret, normally `MESHTASTIC_AGENT_KEY` |
| `agent.model` | Model id, `${AGENT_MODEL:-hermes-agent}` — defaults to `hermes-agent` when unset |
| `agent.timeout` | Per-request timeout; 60 s suits a full agent turn |
| `agent.max_retries` | Retries on 5xx/429/timeout, default 1 |
| `agent.session_prefix` | Prefix of the per-node session id, so each handset gets one conversation |
| `agent.system_prompt` | Tells the agent it is on a radio link, states the byte budget, and asks for a short answer plus a pointer when the full one does not fit. `{max_bytes}` and `{from_id}` are filled from config |
| `agent.shorten_retry` | When a reply still overshoots the budget, ask the agent to rewrite it once before truncating (extra tokens, no extra radio airtime; default true) |
| `agent.ignore_patterns` | Automated notices to drop before any agent call |
| `meshtastic.direct_messages_only` | Answer only messages addressed to this node (default true) |
| `meshtastic.allowed_nodes` | Node ids allowed to reach the agent; empty means anyone (warns at startup) |
| `meshtastic.max_message_length` | Byte budget for replies, default 200 |
| `meshtastic.want_ack`, `meshtastic.ack_timeout` | Request delivery ACKs on unicast replies and warn when none arrives |
| `bridge.host`, `bridge.http_port` | Bridge HTTP API bind, default `0.0.0.0:8085` |

## 4. Where secrets live

Secrets and host-specific values live in `/etc/meshtastic-bridge/.env`
(root-owned, mode 0600) once Path B has run. The repo template `.env.example`
is what gets copied there on first install. Any `${VAR}` or `${VAR:-default}`
reference in `config.yaml` is resolved from this file's environment at startup.

Rules:

- Names have to be **UPPER_SNAKE_CASE**. Lowercase or mixed-case names are
  ignored on purpose, so the substitution never eats the `{max_bytes}` /
  `{from_id}` `.format()` placeholders in `agent.system_prompt`.
- An unset `${VAR}` (no default) stays as the literal `${VAR}` in the parsed
  config. Missing values fail loudly later (URL parse error, agent 404,
  validation message) instead of silently becoming `""`.
- `${VAR:-default}` (shell-style) substitutes `default` only when `VAR` is
  unset. A set-but-empty value is treated as set, so the default does not
  apply.

```bash
MESHTASTIC_AGENT_KEY=<paste the API_SERVER_KEY from the agent host>
AGENT_HOST=<127.0.0.1 | agent.lan>
AGENT_PORT=8642
# AGENT_MODEL=qwen3.7-plus
# SERIAL_PORT=/dev/ttyUSB0
```

## 5. Path A — run as a command (development / one-off)

Use this on a laptop or any host where you want to start the bridge by hand
without systemd. The systemd unit is not involved; export the variables in
your shell and run the binary.

```bash
cd <repo>
python3 -m venv .venv && .venv/bin/pip install -e .
```

Then, in the same shell:

```bash
export MESHTASTIC_AGENT_KEY=<api-server-key>
export AGENT_HOST=192.168.0.100     # or 127.0.0.1 when the agent is on this host
export AGENT_PORT=8642
.venv/bin/meshtastic-bridge
```

`pip install -e .` registers a `meshtastic-bridge` console script. Useful
flags:

- `--config PATH` — load a different `config.yaml`. Without it, the loader
  checks `MESHTASTIC_BRIDGE_CONFIG` then `~/.config/meshtastic-bridge/config.yaml`
  then `./config.yaml`.
- `--check` — read the config, report the transport, agent reachability and
  policy, then exit. Does **not** open the radio. Safe to run any time.

To keep this running unattended, point the systemd unit's `ExecStart` at the
venv binary you just built.

## 6. Path B — installer + systemd (Debian hosts, fixed SBC)

Use this on a Radxa, Pi, or any Debian-family host that should come up after
reboot and stay up without an interactive shell.

```bash
cd <repo>
sudo MESHTASTIC_AGENT_KEY=<key> AGENT_HOST=192.168.0.100 AGENT_PORT=8642 \
  bash deploy/install.sh
sudo systemctl restart meshtastic-bridge
```

`MESHTASTIC_AGENT_KEY` plus any of `AGENT_HOST` / `AGENT_PORT` / `AGENT_MODEL` /
`SERIAL_PORT` exported in the shell are written into
`/etc/meshtastic-bridge/.env` by the installer. On a first install the
file is seeded from `.env.example` first; on a re-install, existing values
in the file are kept unless you re-export them — shell values win. Restart
the service to pick them up.

What the installer does, in order:

1. Verifies `/dev/ttyUSB0`, Python ≥ 3.10, and `systemctl`.
2. Installs `python3-venv`.
3. Creates the `meshtastic` system user (group `dialout`) and
   `/var/log/meshtastic-bridge`.
4. Copies the code to `/opt/meshtastic-bridge` and installs deps in a venv.
5. Installs `/etc/meshtastic-bridge/config.yaml` **only if absent** (your
   edits are never overwritten).
6. Installs `/etc/meshtastic-bridge/.env` **only if absent**, copying from
   `.env.example`. Then writes any `MESHTASTIC_AGENT_KEY` / `AGENT_HOST` /
   `AGENT_PORT` / `AGENT_MODEL` / `SERIAL_PORT` exported in the installer's
   shell on top — shell values win, existing file values are kept otherwise.
   Mode 0600, root-owned.
7. Installs the systemd unit with `EnvironmentFile=` pointing at `.env`.
8. Enables and restarts the service, polls `/health` for up to 30 s, then
   probes the agent's `/health`.

After the installer finishes, edit `/etc/meshtastic-bridge/.env` to set
`AGENT_HOST` (your topology choice) and `AGENT_PORT` (likely `8642`), then
restart the service so it re-reads the env file.

The installer is idempotent. Re-run it to upgrade the code:

```bash
sudo cp config.yaml /etc/meshtastic-bridge/config.yaml   # only if you changed it
sudo bash deploy/install.sh
```

It will not overwrite your `config.yaml` or `.env`, but a fresh deploy
will pick up the new code at `/opt/meshtastic-bridge`.

## 7. Verify

```bash
# Pre-flight: config validity, transport, agent reachability, policy.
# Never opens the radio, so it is safe to run any time.
.venv/bin/meshtastic-bridge --check --config config.yaml
# or, without the venv activated:
.venv/bin/python -m bridge.main --check --config config.yaml

# Live: tail the bridge logs and confirm it bound the HTTP API.
sudo journalctl -u meshtastic-bridge -f

# Health: serial link state, packet counters, agent reachability.
curl -s http://RADIO-HOST:8085/health | jq

# Bundled: same plus an optional probe send if you pass a destination id.
bash scripts/verify_bridge.sh <radio-host>:8085 [!node-id]
```

`--check` reports, in order: config validity, the configured transport and
whether its device is there, the agent URL and whether `/health` answered,
and the inbound policy. Exit code 0 means PASS.

Then prove the loop with real radios: send a direct message to the bridge's
node from a handset and watch the journal on the radio host:

```
Received from !xxxx        -> the radio heard you
Agent replied to !xxxx     -> the agent answered
Sent message to !xxxx      -> the reply went out
No routing ACK from !xxxx  -> the handset never confirmed; suspect its receiver
```

No reply at all?
`.agents/skills/meshtastic-bridge/references/troubleshooting.md` walks through
it: the `/health` fields, the deaf-receiver check, and the rejection log
lines that name an unknown sender.

## 8. Uninstall

```bash
sudo bash deploy/revert.sh    # stops the service, removes the unit, the venv, the env file
```

The repo checkout is left in place; delete it manually if you want a clean
slate.