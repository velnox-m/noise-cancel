#!/usr/bin/env bash
#
# install.sh — installer for "Noise Cancel" (Fedora Workstation / PipeWire)
#
# What this does:
#   1. Installs the small set of system packages needed (GTK bindings + LADSPA base).
#   2. Downloads the RNNoise LADSPA plugin (werman/noise-suppression-for-voice)
#      into your HOME directory — no root/sudo needed for this part.
#   3. Installs the app itself into ~/.local/share/noisecancel-fedora
#   4. Adds an application launcher (shows up in your app grid as "Noise Cancel").
#
# Safe to re-run.

set -euo pipefail

APP_NAME="Noise Cancel"
APP_DIR="$HOME/.local/share/noisecancel-fedora"
BIN_DIR="$HOME/.local/bin"
PLUGIN_DIR="$HOME/.local/lib/ladspa"
CONFIG_DIR="$HOME/.config/noisecancel-fedora"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

# Pinned to a specific, verified release (not "latest") so the checksum
# below always matches what gets downloaded — protects against a corrupted
# download or a tampered file being silently loaded into your audio graph.
RNNOISE_ZIP_URL="https://github.com/werman/noise-suppression-for-voice/releases/download/v1.10/linux-rnnoise.zip"
RNNOISE_ZIP_SHA256="811390b6eb6e28dde023c70590c74d26e74ebb2e595bcf4b95af2341db160e99"

info()  { echo -e "\033[1;34m[*]\033[0m $1"; }
ok()    { echo -e "\033[1;32m[OK]\033[0m $1"; }
warn()  { echo -e "\033[1;33m[!]\033[0m $1"; }
fail()  { echo -e "\033[1;31m[FAIL]\033[0m $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# 1. System dependencies (this is the only part that needs sudo)
# ---------------------------------------------------------------------------
info "Checking system packages (python3-gobject, gtk3, pipewire-utils)..."

NEEDED_RPMS=()
python3 -c "import gi" >/dev/null 2>&1 || NEEDED_RPMS+=("python3-gobject")
rpm -q gtk3 >/dev/null 2>&1 || NEEDED_RPMS+=("gtk3")
command -v wpctl >/dev/null 2>&1 || NEEDED_RPMS+=("pipewire-utils")
command -v pactl  >/dev/null 2>&1 || NEEDED_RPMS+=("pipewire-pulseaudio")
command -v unzip  >/dev/null 2>&1 || NEEDED_RPMS+=("unzip")
command -v curl   >/dev/null 2>&1 || NEEDED_RPMS+=("curl")

if [ "${#NEEDED_RPMS[@]}" -gt 0 ]; then
    info "Installing: ${NEEDED_RPMS[*]} (will ask for your password)"
    sudo dnf install -y "${NEEDED_RPMS[@]}" || fail "Package install failed. Install these manually: ${NEEDED_RPMS[*]}"
else
    ok "All system packages already present."
fi

# ---------------------------------------------------------------------------
# 2. RNNoise LADSPA plugin (no root needed — installed under $HOME)
# ---------------------------------------------------------------------------
mkdir -p "$PLUGIN_DIR"
PLUGIN_SO="$PLUGIN_DIR/librnnoise_ladspa.so"

if [ -f "$PLUGIN_SO" ]; then
    ok "RNNoise plugin already installed at $PLUGIN_SO"
else
    info "Downloading the RNNoise noise-suppression plugin..."
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT

    if ! curl -fsSL "$RNNOISE_ZIP_URL" -o "$TMP/rnnoise.zip"; then
        warn "Could not download the plugin directly."
        warn "Try installing it via Fedora Copr instead, then re-run this script:"
        echo "    sudo dnf copr enable -y ycollet/audinux"
        echo "    sudo dnf install -y ladspa-noise-suppression-for-voice"
        fail "Plugin download failed."
    fi

    info "Verifying checksum..."
    ACTUAL_SHA256=$(sha256sum "$TMP/rnnoise.zip" | awk '{print $1}')
    if [ "$ACTUAL_SHA256" != "$RNNOISE_ZIP_SHA256" ]; then
        fail "Checksum mismatch! Expected $RNNOISE_ZIP_SHA256 but got $ACTUAL_SHA256.
      This means the download was corrupted or tampered with — refusing to
      install it. Nothing was changed on your system. Please re-run this
      script; if it fails again, do not proceed without investigating."
    fi
    ok "Checksum verified."

    unzip -q "$TMP/rnnoise.zip" -d "$TMP/extracted"
    FOUND_SO=$(find "$TMP/extracted" -iname "librnnoise_ladspa.so" | head -n1)
    if [ -z "$FOUND_SO" ]; then
        fail "Downloaded archive did not contain librnnoise_ladspa.so"
    fi
    cp "$FOUND_SO" "$PLUGIN_SO"
    chmod +x "$PLUGIN_SO"
    ok "Installed plugin to $PLUGIN_SO"
    rm -rf "$TMP"
    trap - EXIT
fi

# ---------------------------------------------------------------------------
# 3. Install the app
# ---------------------------------------------------------------------------
info "Installing app files to $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR" "$CONFIG_DIR" "$DESKTOP_DIR" "$ICON_DIR"

cp "$SCRIPT_DIR/noise_cancel.py" "$APP_DIR/noise_cancel.py"
if [ -f "$SCRIPT_DIR/icon.svg" ]; then
    cp "$SCRIPT_DIR/icon.svg" "$ICON_DIR/noisecancel-fedora.svg"
else
    warn "icon.svg not found in $SCRIPT_DIR, skipping icon copy."
fi

echo "$PLUGIN_SO" > "$CONFIG_DIR/plugin_path.txt"

# Any leftover config from an older version of this app that embedded the
# filter directly in the main PipeWire daemon is cleaned up automatically
# by noise_cancel.py itself the next time it runs — nothing to do here.

cat > "$BIN_DIR/noisecancel-fedora" <<EOF
#!/usr/bin/env bash
exec python3 "$APP_DIR/noise_cancel.py" "\$@"
EOF
chmod +x "$BIN_DIR/noisecancel-fedora"

cat > "$DESKTOP_DIR/noisecancel-fedora.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Noise Cancel
Comment=One-click microphone background noise removal
Exec=$BIN_DIR/noisecancel-fedora
Icon=noisecancel-fedora
Terminal=false
Categories=AudioVideo;Audio;Utility;
StartupNotify=true
EOF

update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

# Make sure $HOME/.local/bin is on PATH (usually already is on Fedora)
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) warn "$HOME/.local/bin is not on your PATH. Launch from the app grid, or add it to PATH." ;;
esac

ok "Installation complete!"
echo
echo "  Launch it from your app grid — search for \"Noise Cancel\""
echo "  or run:  noisecancel-fedora"
echo
