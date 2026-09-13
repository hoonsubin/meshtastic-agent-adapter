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