#!/usr/bin/env bash
# Removes Noise Cancel and restores your normal microphone.
set -euo pipefail

HOME_DIR="$HOME"
CONFIG_DIR="$HOME_DIR/.config/noisecancel-fedora"
SERVICE_FILE="$HOME_DIR/.config/systemd/user/noisecancel-fedora.service"
LEGACY_PIPEWIRE_CONF="$HOME_DIR/.config/pipewire/pipewire.conf.d/99-noisecancel-fedora.conf"

echo "[*] Restoring default microphone..."
CURRENT_DEFAULT="$(pactl get-default-source 2>/dev/null || true)"
if [ "$CURRENT_DEFAULT" = "noisecancel_fedora_mic" ]; then
    # Best effort: pick any other real source as the new default.
    FALLBACK="$(pactl list short sources 2>/dev/null | awk -F'\t' '$2 !~ /\.monitor$/ && $2 != "noisecancel_fedora_mic" {print $2; exit}')"
    [ -n "$FALLBACK" ] && pactl set-default-source "$FALLBACK" 2>/dev/null || true
fi

echo "[*] Stopping and removing the Noise Cancel service..."
systemctl --user stop noisecancel-fedora.service 2>/dev/null || true
rm -f "$SERVICE_FILE"
systemctl --user daemon-reload 2>/dev/null || true

if [ -f "$LEGACY_PIPEWIRE_CONF" ]; then
    echo "[*] Removing leftover config from an older version..."
    rm -f "$LEGACY_PIPEWIRE_CONF"
    systemctl --user restart pipewire.service wireplumber.service pipewire-pulse.service 2>/dev/null || true
fi

echo "[*] Removing app files..."
rm -rf "$HOME_DIR/.local/share/noisecancel-fedora"
rm -f "$HOME_DIR/.local/bin/noisecancel-fedora"
rm -f "$HOME_DIR/.local/share/applications/noisecancel-fedora.desktop"
rm -f "$HOME_DIR/.local/share/icons/hicolor/scalable/apps/noisecancel-fedora.svg"
rm -f "$HOME_DIR/.config/autostart/noisecancel-fedora.desktop"
rm -rf "$CONFIG_DIR"

echo "[*] (Kept: the RNNoise plugin at ~/.local/lib/ladspa — harmless to leave, delete manually if you want.)"
echo "[OK] Noise Cancel removed."
