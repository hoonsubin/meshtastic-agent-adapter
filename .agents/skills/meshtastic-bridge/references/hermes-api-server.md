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

```
terminal(command="KEY=$(grep '^API_SERVER_KEY=' ~/.hermes/.env | cut -d= -f2-); curl -s -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:8642/v1/models; curl -s -H \"Authorization: Bearer $KEY\" http://127.0.0.1:8642/v1/models")
```

`401` without the key and `200` with it is the healthy pair. From the radio host,
`curl -s http://<hermes-host>:8642/health` must answer `200` for the bridge to
work at all.

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

## Related knobs

- `gateway.api_server.max_concurrent_runs` (default 10) caps runs and answers
  `429` beyond it; the bridge treats 5xx and 429 as retryable, one retry only.
- Under `gateway.multiplex_profiles`, secondary profiles answer under
  `/p/<profile>/...`; the single-profile path above is what this bridge expects.
