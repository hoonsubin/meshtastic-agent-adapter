# Enabling the Hermes API server (any instance)

The bridge needs one HTTP endpoint on the Hermes side. This is how to turn it on
for any Hermes instance, and what to check when it is not answering.

## Enable

Two values in `~/.hermes/.env` (secrets live in .env, not config.yaml):

```
API_SERVER_KEY=<strong random key>
API_SERVER_HOST=0.0.0.0        # or the LAN address, when the bridge is on another host
```

`API_SERVER_KEY` is what turns the platform on: with no usable key there is no
listener at all. The default bind is loopback, so a bridge on another machine
needs `API_SERVER_HOST`.

Then restart the gateway and confirm:

```
terminal(command="hermes gateway restart")
terminal(command="ss -tlnp | grep 8642")
terminal(command="journalctl --user -u hermes-gateway --since '-2min' | grep -i api_server")
```

Expected log line: `[Api_Server] API server listening on http://0.0.0.0:8642`.
Port default is 8642 (`API_SERVER_PORT` overrides it).

## Endpoints the bridge uses

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | none | Reachability probe behind `agent_reachable` |
| `GET /v1/models` | Bearer | Reports the model id, by default `hermes-agent` |
| `POST /v1/chat/completions` | Bearer | The actual request/reply call |

Request shape (what the bridge sends):

```
POST /v1/chat/completions
Authorization: Bearer <API_SERVER_KEY>
X-Hermes-Session-Id: <session_prefix>-<node id>
Content-Type: application/json

{"model": "hermes-agent",
 "messages": [{"role": "system", "content": "..."},
              {"role": "user", "content": "<the LoRa message>"}]}
```

The reply is read from `choices[0].message.content`.

`X-Hermes-Session-Id` gives one conversation per mesh node (the API server
threads sessions on that header with a 7 day idle window). Without it every
message is an isolated turn.

## Verify before touching the bridge

From the Hermes host (sanity check the API server itself, no agent involvement):

```
terminal(command="ss -tlnp | grep 8642")
terminal(command="curl -s -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:8642/v1/models")
terminal(command="KEY=$(grep '^API_SERVER_KEY=' ~/.hermes/.env | cut -d= -f2-); curl -s -o /dev/null -w '%{http_code}\\n' -H \"Authorization: Bearer ***\" http://127.0.0.1:8642/v1/models")
```

`401` without the key and `200` with it is the healthy pair.

From the **radio host**, confirm the bridge can actually reach the agent
(this is the single most common failure mode — wrong `AGENT_HOST`, firewalled
port, Hermes bound to localhost):

```
terminal(command="curl -sv --max-time 5 http://<hermes-host>:8642/health 2>&1 | grep -E 'Connected|HTTP/|Try connecting'")
```

The bridge cannot start serving until `/health` answers 200 from the radio host.

## Security (do not skip)

The key grants full agent access on the Hermes host, and agent work dispatched
through this endpoint runs as that user. With `terminal.backend: local` (the
common case) that is an unsandboxed shell.

- The gateway logs a warning on every start when the endpoint is network
  accessible with a local terminal backend. Firewall port 8642 to the bridge host
  and the tailnet, or bind to a specific interface.
- One key serves every client: rotating it means updating
  `~/.hermes/.env` and every bridge's env file.
- The bridge's own HTTP API (`/send`, `/callback`, port 8085) is unauthenticated
  by design; treat it the same way and keep it off untrusted networks.

## Rotating the API key

The order matters: if you update the bridge before the Hermes side, every
agent call gets `401` for the gap. The right order is:

1. On the **Hermes host**: replace `API_SERVER_KEY` in `~/.hermes/.env` with
   the new value, then `hermes gateway restart`. Confirm the
   `[Api_Server] API server listening on http://0.0.0.0:8642` line in
   `journalctl --user -u hermes-gateway --since '-1min'`.
2. On **each bridge host**: replace `MESHTASTIC_AGENT_KEY` in
   `/etc/meshtastic-bridge/.env` with the same new value, then
   `sudo systemctl restart meshtastic-bridge`. Wait ~10s (DTR/RTS reboot)
   before judging `/health`.
3. Run `bash scripts/verify_bridge.sh <radio-host>:8085` against each bridge.
   `agent_reachable: true` plus a 200 from `/v1/models` on the Hermes side
   means the rotation is complete.

There is no in-band rotation handshake; the bridge reads `MESHTASTIC_AGENT_KEY`
from its env file at every request, so step 2 above is sufficient — the service
does not need to be reinstalled.

## Related knobs

- `gateway.api_server.max_concurrent_runs` (default 10) caps runs and answers
  `429` beyond it; the bridge treats 5xx and 429 as retryable, one retry only.
- Under `gateway.multiplex_profiles`, secondary profiles answer under
  `/p/<profile>/...`; the single-profile path above is what this bridge expects.
