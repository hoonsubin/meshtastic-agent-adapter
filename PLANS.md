# Implementation Plans and Status

## Architecture (shipped)

```
LoRa -> bridge (raxda-dietpi, /dev/ttyUSB0) -> POST {agent.url} with Bearer
     -> choices[0].message.content -> truncate to LoRa bytes -> send back to sender
```

Agent-agnostic HTTP, one request per message, per-node session continuity via
`X-Hermes-Session-Id`. Details and rationale: README.md, deployment facts:
DEPLOYMENT.md.

## Phase 1: Core bridge service (DONE)

Standalone async service that shuttles messages between serial and HTTP.

- [x] `bridge/main.py` orchestrator, signal handling, reply hop
- [x] `bridge/config.py` YAML config, env-based secret, fail-closed validation
- [x] `bridge/radio.py` MeshtasticRadio (pubsub receive, async send, delivery ACKs) over `bridge/transport.py` (serial today; the registry is the seam for another connection method)
- [x] `bridge/http_server.py` `/send`, `/callback`, `/health`
- [x] `bridge/http_client.py` OpenAI-compatible request/reply client
- [x] `bridge/text.py` byte-safe truncation
- [x] Tests (37)
- [x] Deployed on raxda-dietpi, service enabled, health green

## Phase 2: Hermes integration (DONE, approach changed)

Originally planned as a webhook route plus a platform-adapter plugin. That
design failed: every inbound POST was rejected 401 "Invalid signature", and the
layering produced two competing delivery paths. See README "Why this design".

- [x] Replaced with the API-server bridge (Bearer auth, synchronous reply)
- [x] Per-node session continuity (`X-Hermes-Session-Id`)
- [x] Removed the webhook `meshtastic` route, the `meshtastic` platform block and
      the plugin entry from `~/.hermes/config.yaml`
- [x] End-to-end verified over real LoRa with two nodes

## Phase 3: Agent-agnostic modularity

Goal: any HTTP agent, not just Hermes, can drive the bridge.

- [x] Config-driven agent endpoint (`agent.url`, `agent.model`, `agent.api_key_env`)
- [ ] Backend abstraction for non-OpenAI agent APIs
- [ ] Routing rules (per node, per channel -> different agents)
- [ ] LAN discovery (mDNS)

## Phase 4: Robustness and monitoring

- [x] Retry with backoff for the agent request (one retry, then stay silent)
- [x] `/health` reports serial and agent reachability
- [x] Heartbeat via systemd restart policy
- [ ] Message queue persistence across restarts
- [ ] Structured JSON logging, optional Prometheus metrics
- [ ] Dedupe rebroadcast packets (only if the field shows duplicates)

## Phase 5: Advanced features (optional)

- [ ] Multi-channel support
- [ ] Long-reply chunking across several LoRa packets
- [ ] File transfer
- [ ] Voice messages (STT/TTS)

## Removed / retired (2026-09-12)

- `~/.hermes/plugins/meshtastic/` adapter plugin: removed (backup at `~/backups/meshtastic-plugin-20260912-210501.tar.gz`)
- `MESHTASTIC_BRIDGE_SECRET`: dropped from `~/.hermes/.env` (`WEBHOOK_SECRET` stays, the webhook platform still references it)
- `scripts/api_server_bridge.py`, `scripts/webhook_poc.py`: deleted, both superseded PoCs
- stale `systemd/` duplicate unit and the legacy root `install.sh`: deleted

## Open items

- [ ] **Portable node's RECEIVER is deaf (open, diagnosed 2026-09-12 19:38 UTC)**: one-directional fault. Portable -> relay works (bridge logged "portable-tx-..." at 19:36:50); relay -> portable is dead. In 110s of sniffing with a probe-active relay, the portable heard ZERO packets from any node (only its own telemetry), and it reports `channelUtilization 0.0` where neighbouring nodes report 10-40%, so it hears neither the relay at short range nor the busy Munich mesh. Its TX works, so this is an RX-path fault (antenna/front-end), not software. The power cycle at 19:29 UTC did not fix it, and it is the reason the third reply ("I'm software without a physical GPS position...") never arrived: the bridge transmitted it (19:25:32) and logged success on handoff.
  Next: reseat/replace the antenna on the portable node, then walk it somewhere with mesh traffic and watch `LastHeard` in its node DB. If it stays deaf everywhere, the board/radio is faulty. The detection side is now covered: `last_inbound_seconds` in `/health` and the missing-ACK warning both fire within seconds, and `.hermes/skills/meshtastic-bridge/scripts/mesh_sniffer.py` gives the verdict on the node itself.
- [ ] Live test with the operator's own handset on the operator's node (together, later) - blocked on the item above
- [ ] Firewall the agent API port (8642) to the bridge host and the tailnet (agreed: after the live test); the bridge's own 8085 API is unauthenticated by design and belongs in the same decision
- [ ] Sender allowlist and per-node rate limit: anything that can reach the channel can drive the agent today, and each message costs a full turn
- [ ] Rebroadcast dedupe: still not observed in the field, add only if duplicates ever appear

## Done in the live-test review pass (2026-09-12)

- [x] Delivery verification: `wantAck` on unicast replies plus a missing-ACK WARNING (validated live against the deaf node)
- [x] RX liveness: `/health` reports `inbound_packets`/`last_inbound_seconds` (from other nodes) and `own_packets` (this node's own echoes), so a deaf radio is distinguishable from a working one
- [x] Marked, word-aware truncation (trailing `...` inside the byte budget)
- [x] `agent.ignore_patterns` so automated traffic (position shares) costs no agent turn
- [x] Portable agent skill set committed at `.hermes/skills/meshtastic-bridge/` (SKILL.md, 3 references, 3 scripts incl. a deaf-receiver verdict sniffer)
- [x] Direct messages only by default (`meshtastic.direct_messages_only`), so the agent never sees channel-wide traffic; direct messages are PKI-encrypted to the recipient
- [x] Sender allowlist (`meshtastic.allowed_nodes`), seeded with the portable node, with rate-limited rejection logging that names unknown senders
- [ ] Live validation of the DM filter and the allowlist against a real handset, once a node is plugged back in (the portable is currently unplugged from the workstation, so `inbound_packets` is legitimately 0 while `own_packets` climbs)
