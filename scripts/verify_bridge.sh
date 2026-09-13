#!/usr/bin/env bash
#
# Verify a deployed Meshtastic bridge from anywhere on the LAN.
#
# Usage:
#   bash verify_bridge.sh [BRIDGE] [DESTINATION] [MESSAGE]
#
#   BRIDGE       host:port of the bridge HTTP API (default 127.0.0.1:8085)
#   DESTINATION  optional node id for a probe send, e.g. '!68916e4c'
#   MESSAGE      optional probe text (implies DESTINATION)
#
# Exits non-zero if the bridge is not healthy, so it can be used as a check.
set -uo pipefail

BRIDGE="${1:-127.0.0.1:8085}"
DESTINATION="${2:-}"
MESSAGE="${3:-bridge-verify}"

fails=0
warns=0
say_ok()   { printf '  OK   %s\n' "$1"; }
say_warn() { printf '  WARN %s\n' "$1"; warns=$((warns + 1)); }
say_fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

echo "=== Bridge verification: $BRIDGE ==="

health=$(curl -s --max-time 8 "http://$BRIDGE/health" || true)
if [[ -z "$health" ]]; then
    say_fail "no answer from http://$BRIDGE/health (service down, wrong host, firewall?)"
    echo
    echo "RESULT: FAIL"
    exit 1
fi
echo "  payload: $health"

read_field() {
    printf '%s' "$health" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('$1', ''))
except Exception:
    print('')
" 2>/dev/null
}

serial=$(read_field serial)
agent=$(read_field agent_reachable)
packets=$(read_field inbound_packets)
last=$(read_field last_inbound_seconds)
own=$(read_field own_packets)

[[ "$serial" == "True" ]] && say_ok "serial connected" || say_fail "serial=$serial (node unplugged, busy, or wrong serial.port)"
[[ "$agent" == "True" ]] && say_ok "agent API reachable" || say_fail "agent_reachable=$agent (API server off, wrong agent.url, or firewall)"

# inbound_packets counts packets from OTHER nodes, own_packets this node's own
# echoes, so an idle-but-healthy bridge is distinguishable from a deaf one.
if [[ "$last" != "None" && -n "$last" && "$last" -lt 120 ]]; then
    say_ok "radio heard another node ${last}s ago (inbound_packets=$packets)"
elif [[ -n "$own" && "$own" != "0" && "$own" != "None" ]]; then
    say_warn "no packets from other nodes yet (inbound_packets=$packets, own_packets=$own):"
    echo "       the serial link and this node's TX work, so either nobody is"
    echo "       transmitting to this bridge or its receiver is deaf. Send a direct"
    echo "       message from a handset and re-run; if nothing arrives, run"
    echo "       mesh_sniffer.py on the node to settle it."
else
    say_fail "no packets seen at all (inbound_packets=$packets, own_packets=$own): serial link or radio trouble"
fi

if [[ -n "$DESTINATION" ]]; then
    echo
    echo "  probe send to $DESTINATION..."
    probe=$(curl -s --max-time 20 -X POST "http://$BRIDGE/send" \
        -H 'Content-Type: application/json' \
        -d "{\"destination_id\":\"$DESTINATION\",\"message\":\"$MESSAGE\",\"channel\":0}" || true)
    echo "  response: $probe"
    if printf '%s' "$probe" | grep -q '"status": *"sent"'; then
        say_ok "bridge accepted the send (watch the log for a routing ACK)"
    else
        say_fail "bridge refused the send: $probe"
    fi
    echo
    echo "  confirm delivery in the log on the radio host:"
    echo "    sudo journalctl -u meshtastic-bridge --since -5min | grep -E 'Sent message|No routing ACK'"
fi

echo
if [[ "$fails" -eq 0 && "$warns" -eq 0 ]]; then
    echo "RESULT: PASS"
    exit 0
fi
if [[ "$fails" -eq 0 ]]; then
    echo "RESULT: PASS ($warns warning(s) - see above)"
    exit 0
fi
echo "RESULT: FAIL ($fails check(s), $warns warning(s))"
exit 1
