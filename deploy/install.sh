#!/usr/bin/env bash
#
# Install the Meshtastic Bridge on a DietPi/Debian node.
#
# Usage:
#   sudo MESHTASTIC_AGENT_KEY=<key> bash deploy/install.sh   # first install
#   sudo bash deploy/install.sh                             # re-install/upgrade
#
# What it does:
#   0. Pre-flight hardware verification (serial port, python, systemd)
#   1. Installs system dependencies (Python 3, venv, pip)
#   2. Creates the service user and log directory
#   3. Copies bridge code to /opt/meshtastic-bridge and installs deps in a venv
#   4. Installs config to /etc/meshtastic-bridge/config.yaml (never overwrites)
#   5. Installs the agent API key in /etc/meshtastic-bridge/.env (0600)
#   6. Installs the systemd unit
#   7. Enables and restarts the service
#   8. Post-install verification (agent reachability, bridge health)
set -euo pipefail

INSTALL_DIR="/opt/meshtastic-bridge"
CONFIG_DIR="/etc/meshtastic-bridge"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
ENV_FILE="$CONFIG_DIR/.env"
SERVICE_USER="meshtastic"
SERVICE_NAME="meshtastic-bridge"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Env-file handling and ${VAR} expansion live in bridge/environment.py
# (stdlib-only, so the system python3 runs it before the bridge venv exists).
env_tool() {
    python3 "$REPO_DIR/bridge/environment.py" "$@"
}

echo "=== Meshtastic Bridge Installer ==="

# Must run as root
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run as root (sudo bash deploy/install.sh)"
    exit 1
fi

# 0. Pre-flight hardware verification
echo "[0/8] Pre-flight hardware verification..."

if [[ ! -e /dev/ttyUSB0 ]]; then
    echo "ERROR: Serial port /dev/ttyUSB0 not found"
    echo "  - Is the Meshtastic node connected via USB?"
    echo "  - Check with: ls -la /dev/ttyUSB*"
    exit 1
fi
echo "  ✓ Serial port /dev/ttyUSB0 exists"

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ $PYTHON_MAJOR -lt 3 ]] || [[ $PYTHON_MINOR -lt 10 ]]; then
    echo "ERROR: Python 3.10+ required, found $PYTHON_VERSION"
    exit 1
fi
echo "  ✓ Python $PYTHON_VERSION"

if ! command -v systemctl &>/dev/null; then
    echo "ERROR: systemd not found"
    exit 1
fi
echo "  ✓ systemd available"

echo ""

# 1. System dependencies
echo "[1/8] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

# 2. Service user and log directory
echo "[2/8] Creating service user and log directory..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
usermod -aG dialout "$SERVICE_USER"

mkdir -p /var/log/meshtastic-bridge
chown "$SERVICE_USER:$SERVICE_USER" /var/log/meshtastic-bridge

# 3. Bridge code and venv
echo "[3/8] Installing bridge to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r "$REPO_DIR/bridge" "$INSTALL_DIR/"
cp "$REPO_DIR/pyproject.toml" "$INSTALL_DIR/"
cp "$REPO_DIR/requirements.txt" "$INSTALL_DIR/"

if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# 4. Config (never overwrite an existing deployed config)
echo "[4/8] Config file..."
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_FILE" ]]; then
    cp "$REPO_DIR/config.yaml" "$CONFIG_FILE"
    echo "  Installed $CONFIG_FILE from the repo (review the agent.url!)"
else
    echo "  $CONFIG_FILE already exists, preserved"
fi
if grep -qE '^[[:space:]]*secret:' "$CONFIG_FILE"; then
    echo "  WARNING: $CONFIG_FILE still contains a legacy agent.secret."
    echo "           It is unused now: the API key comes from $ENV_FILE."
fi

# 5. Env file (.env, mode 0600) — secrets + host-specific values
# referenced as ${VAR} in config.yaml. The installer seeds it from
# .env.example on first run, then writes any host-specific vars the
# operator exported in the shell, then writes MESHTASTIC_AGENT_KEY. Re-runs
# preserve any value the operator already set in $ENV_FILE.
echo "[5/8] Env file (.env, mode 0600)..."
umask 077
env_tool seed --file "$ENV_FILE" --template "$REPO_DIR/.env.example"
env_tool capture --file "$ENV_FILE"
env_tool set-key --file "$ENV_FILE"
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# 6. systemd unit
echo "[6/8] Installing systemd service..."
cp "$REPO_DIR/deploy/meshtastic-bridge.service" /etc/systemd/system/
systemctl daemon-reload

# Read the deployed config values: the wait loop and the verification both need them.
# env_tool expand applies the same ${VAR} substitution as bridge.config.Config.load,
# so the post-install checks see the resolved URL, not the raw placeholder.
AGENT_URL=$(env_tool expand < "$CONFIG_FILE" | "$INSTALL_DIR/venv/bin/python" -c '
import sys, yaml
try:
    data = yaml.safe_load(sys.stdin) or {}
    print((data.get("agent") or {}).get("url", ""))
except Exception:
    print("")
')

HTTP_PORT=$(env_tool expand < "$CONFIG_FILE" | "$INSTALL_DIR/venv/bin/python" -c '
import sys, yaml
try:
    data = yaml.safe_load(sys.stdin) or {}
    print((data.get("bridge") or {}).get("http_port", 8085))
except Exception:
    print(8085)
')

# 7. Enable and (re)start
echo "[7/8] Enabling and restarting the service..."
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"

# The node's serial handshake takes several seconds before the HTTP server binds,
# so poll for it instead of guessing a fixed delay.
BRIDGE_UP=0
for _ in $(seq 1 30); do
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        echo "  ERROR: service is not active. Recent log lines:"
        journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true
        exit 1
    fi
    if curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:${HTTP_PORT}/health"; then
        BRIDGE_UP=1
        break
    fi
    sleep 1
done

if [[ "$BRIDGE_UP" != "1" ]]; then
    echo "  ERROR: bridge HTTP API did not answer on port ${HTTP_PORT} within 30s:"
    journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true
    exit 1
fi
echo "  Service active, bridge HTTP API responding on port ${HTTP_PORT}"

# 8. Post-install verification
echo "[8/8] Verification..."

if [[ -n "$AGENT_URL" ]]; then
    AGENT_BASE="${AGENT_URL%%/v1/*}"
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$AGENT_BASE/health" || echo "000")
    if [[ "$CODE" == "200" ]]; then
        echo "  ✓ Agent API reachable at $AGENT_BASE/health"
    else
        echo "  ⚠ Agent API at $AGENT_BASE/health returned $CODE"
        echo "    Check agent.url, the LAN route, and the API_SERVER_HOST on the agent host."
    fi
else
    echo "  ⚠ Could not read agent.url from $CONFIG_FILE"
fi

echo "  Bridge health:"
curl -s --max-time 5 "http://127.0.0.1:${HTTP_PORT}/health" || echo "  (no response)"
echo ""

echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Check config: sudo nano $CONFIG_FILE"
echo "  2. Watch logs:   sudo journalctl -u $SERVICE_NAME -f"
echo "  3. Send a test message from a mesh handset and watch it answer"
echo "  4. Uninstall:    sudo bash $REPO_DIR/deploy/revert.sh"
