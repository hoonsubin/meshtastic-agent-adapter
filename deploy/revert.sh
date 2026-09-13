#!/usr/bin/env bash
#
# Revert Meshtastic Bridge installation
#
set -euo pipefail

INSTALL_DIR="/opt/meshtastic-bridge"
CONFIG_DIR="/etc/meshtastic-bridge"
SERVICE_USER="meshtastic"
SERVICE_NAME="meshtastic-bridge"

echo "=== Reverting Meshtastic Bridge Installation ==="

# Must run as root
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run as root (sudo bash revert.sh)"
    exit 1
fi

# 1. Stop and disable service
echo "[1/5] Stopping service..."
if systemctl is-active --quiet "$SERVICE_NAME"; then
    systemctl stop "$SERVICE_NAME"
    echo "  Service stopped"
else
    echo "  Service not running"
fi

if systemctl is-enabled --quiet "$SERVICE_NAME"; then
    systemctl disable "$SERVICE_NAME"
    echo "  Service disabled"
else
    echo "  Service not enabled"
fi

# 2. Remove systemd service file
echo "[2/5] Removing systemd service..."
if [[ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]]; then
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    echo "  Service file removed"
else
    echo "  Service file not found"
fi

# 3. Remove installation directory
echo "[3/5] Removing installation directory..."
if [[ -d "$INSTALL_DIR" ]]; then
    rm -rf "$INSTALL_DIR"
    echo "  Removed $INSTALL_DIR"
else
    echo "  Directory not found"
fi

# 4. Remove config directory (optional - ask user)
echo "[4/5] Config directory..."
if [[ -d "$CONFIG_DIR" ]]; then
    echo "  NOTE: this also removes $CONFIG_DIR/.env, which holds the agent"
    echo "        API key. Revoke or rotate API_SERVER_KEY on the agent host if the"
    echo "        key has been exposed."
    read -p "Remove $CONFIG_DIR? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$CONFIG_DIR"
        echo "  Config removed"
    else
        echo "  Config preserved"
    fi
else
    echo "  Config directory not found"
fi

# 5. Remove service user (optional - ask user)
echo "[5/5] Service user..."
if id "$SERVICE_USER" &>/dev/null; then
    read -p "Remove user '$SERVICE_USER'? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        userdel "$SERVICE_USER" 2>/dev/null || true
        echo "  User removed"
    else
        echo "  User preserved"
    fi
else
    echo "  User not found"
fi

echo ""
echo "=== Revert complete ==="
echo "The Meshtastic Bridge has been removed."
