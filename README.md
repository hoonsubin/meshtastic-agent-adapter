# Meshtastic Agent Adapter

Have you ever thought, "what if I want to talk to my AI GF without internet?"

Say no more.

This is a bidirectional LoRa messaging bridge between a Meshtastic mesh and
OpenAI-compatible endpoints, over a single synchronous HTTP call. The bridge
is agent-agnostic: anything that speaks OpenAI-compatible chat completions on
the LAN can be on the other end.

This implementation is currently tested on the Hermes Agent. It's in a PoC
stage made for fun.

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

## Quick start

```bash
# On the radio host (Linux with the Meshtastic node on /dev/ttyUSB0):
git clone <this-repo> meshtastic-agent-adapter
cd meshtastic-agent-adapter
python3 -m venv .venv && .venv/bin/pip install -e .

# Find the agent host's API key, then point the bridge at it:
MESHTASTIC_AGENT_KEY=<api-server-key> \
AGENT_HOST=<agent-host> \
AGENT_PORT=8642 \
  .venv/bin/meshtastic-bridge --check --config config.yaml
# exit 0 = config, transport, agent, and inbound policy are all OK
```

For a fixed SBC that should come up on reboot, use the installer instead:

```bash
sudo MESHTASTIC_AGENT_KEY=<api-server-key> \
AGENT_HOST=<agent-host> AGENT_PORT=8642 \
  bash deploy/install.sh
```

Then from anywhere on the LAN:

```bash
curl -s http://<radio-host>:8085/health
bash scripts/verify_bridge.sh <radio-host>:8085
sudo journalctl -u meshtastic-bridge -f
```

`MESHTASTIC_AGENT_KEY` lives in the bridge host's env (systemd
`EnvironmentFile=` at `/etc/meshtastic-bridge/.env`, root-owned, mode 0600),
never in `config.yaml`. The repo's `.env.example` is the template.

## Connection methods

The bridge is transport-agnostic: `bridge/transport.py` registers one class per
connection method. `meshtastic.connection` in `config.yaml` picks one.

### USB serial (the original use case)

```yaml
serial:
  port: /dev/ttyUSB0
  baud: 921600
meshtastic:
  connection: serial
```

The installer hard-requires `/dev/ttyUSB0` to exist on the host. Pairing is
not a thing - the serial port is exclusive for the bridge's lifetime.

### Bluetooth Low Energy (ThinkNode M6, Heltec /w BLE, ...)

```yaml
ble:
  address: "D8:0B:FE:FE:20:F0"
meshtastic:
  connection: ble
```

The bridge holds the only BLE connection the node supports for its lifetime
(NimBLE advertises only while no central is connected). Pairing and trust
happen at the OS level (bluez) **once, before the bridge runs** - the
transport does not pair on its own:

```bash
# Interactive, once per host. The node prompts for its fixed PIN/passkey;
# the default is 123456 for FIXED_PIN pairing mode (see the Meshtastic
# Bluetooth docs: https://meshtastic.org/docs/configuration/radio/bluetooth/).
bluetoothctl pair D8:0B:FE:FE:20:F0
bluetoothctl trust D8:0B:FE:FE:20:F0
```

The installer detects `connection: ble` and skips the `/dev/ttyUSB0` check,
verifies `/sys/class/bluetooth/hci0` is present, and adds the
`meshtastic` service user to the `bluetooth` group (alongside `dialout`)
so bluez DBus calls are allowed.

**Known upstream caveat** (the same one that affects `meshtastic --ble`):
once the bridge holds the connection, the node stops advertising - so any
later `meshtastic --ble <addr>` CLI calls will fail with "BLE device not
found" until the bridge is stopped. Use `scripts/mesh_sniffer.py` (passive,
no connection) or stop the bridge briefly if you need CLI access. Tracked in
`meshtastic/python` issues #972, #777.

## Debugging

### Per-packet RSSI / SNR in the journal

For mesh signal-quality work (is the node hearing the rest of the mesh?
are unicast ACKs missing because of a deaf receiver?), flip
`bridge.log_level: DEBUG` in `config.yaml` and restart the service.
Every received packet then logs one line:

```
rx rssi=-58 snr=8.5 from=!fefe20f0 to=!ffffffff portnum=NEIGHBORINFO_APP
rx rssi=-92 snr=-4.25 from=!68916e4c to=!fefe20f0 portnum=TEXT_MESSAGE_APP
```

- `rssi` is dBm (negative, closer to 0 = louder); -120 is the floor of
  typical LoRa receiver sensitivity, anything above -100 is comfortable.
- `snr` is dB above the noise floor; positive is healthy, anything below
  -5 is starting to lose margin.

Turn it back to `INFO` once you're done - at DEBUG the journal fills up
at ~1 pkt/s with the device-telemetry broadcasts.

### Other quick checks

```bash
# Inbound packet rate + agent reachability (the bridge health endpoint)
curl -s http://<radio-host>:8085/health | jq

# Last 50 lines from the bridge (errors + reconnects show up here)
sudo journalctl -u meshtastic-bridge -n 50 --no-pager

# Passive mesh sniffer: no BLE connection, just LoRa radio listen
sudo scripts/mesh_sniffer.py
```

## For everything else

- **Full setup walkthrough** (preconditions, both topologies, the values
  table, agent-side enablement): `docs/SETUP.md`
- **Deploy, diagnose, repair**: the agent skill shipped at
  `.agents/skills/meshtastic-bridge/`. Trust it on another Hermes instance
  with `hermes skills trust <path-to-this-repo>`. It covers radio-side
  verification, the Hermes API server reference, and a troubleshooting flow
  for the failures that look like software but are not.
- **Why this design** and the abandoned alternatives are in `PLANS.md`.

## Project layout

```
meshtastic-agent-adapter/
├── bridge/                            # the bridge itself
├── deploy/                            # install.sh, revert.sh, systemd unit
├── docs/SETUP.md                      # the full setup guide
├── scripts/                           # verify_bridge.sh, mesh_listen.py, mesh_sniffer.py
├── tests/                             # unit + integration tests (pytest)
├── .agents/skills/meshtastic-bridge/  # portable agent skill (deploy + diagnose)
├── config.yaml                        # source of truth (deployed to /etc/meshtastic-bridge/)
├── .env.example                       # template for /etc/meshtastic-bridge/.env
└── PLANS.md                           # phase status + design history
```

## References

- [Hermes Agent docs](https://hermes-agent.nousresearch.com/docs)
- [Meshtastic Python API](https://meshtastic.org/docs/software/python/python-cli)