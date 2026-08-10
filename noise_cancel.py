#!/usr/bin/env python3
"""
Noise Cancel — a NoiseTorch-style, one-toggle background noise remover
for Fedora Workstation (PipeWire).

Design note (v3): the RNNoise filter runs as its own small, standalone
PipeWire *client* process (via a dedicated systemd --user service),
connecting to your existing PipeWire session the same way any normal app
does — it does NOT get embedded into the main PipeWire daemon's config.
This matters: it means enabling/disabling/reconfiguring noise cancellation
never restarts pipewire.service, pipewire-pulse.service, or wireplumber.service,
so it can't disrupt Bluetooth audio (which wireplumber manages) or cause
any other client's audio to hiccup. Turning it on/off just starts or stops
one small extra process.

No terminal needed for normal use — this file *is* the whole app.
"""

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

import json
import os
import shutil
import subprocess
import sys
import time
import threading

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "noisecancel-fedora")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
PLUGIN_PATH_FILE = os.path.join(CONFIG_DIR, "plugin_path.txt")
FILTER_CONF_FILE = os.path.join(CONFIG_DIR, "filter-chain.conf")

SYSTEMD_USER_DIR = os.path.join(HOME, ".config", "systemd", "user")
SERVICE_NAME = "noisecancel-fedora.service"
SERVICE_FILE = os.path.join(SYSTEMD_USER_DIR, SERVICE_NAME)

# Leftover artifact from older (v1/v2) builds of this app, which embedded
# the filter directly into the main PipeWire daemon's config and required
# restarting pipewire/wireplumber to apply it. That approach could disrupt
# Bluetooth audio. We clean it up on startup if found.
LEGACY_PIPEWIRE_CONF = os.path.join(
    HOME, ".config", "pipewire", "pipewire.conf.d", "99-noisecancel-fedora.conf"
)

AUTOSTART_DIR = os.path.join(HOME, ".config", "autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "noisecancel-fedora.desktop")

NODE_NAME = "noisecancel_fedora_mic"
NODE_DESCRIPTION = "Noise Cancelled Microphone"

DEFAULT_STRENGTH = 90  # VAD Threshold (%) — higher = more aggressive gating


# ---------------------------------------------------------------------------
# Small shell/audio helpers
# ---------------------------------------------------------------------------

def read_plugin_path():
    try:
        with open(PLUGIN_PATH_FILE) as f:
            path = f.read().strip()
        if os.path.isfile(path):
            return path
    except FileNotFoundError:
        pass
    fallback = os.path.join(HOME, ".local", "lib", "ladspa", "librnnoise_ladspa.so")
    if os.path.isfile(fallback):
        return fallback
    return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "enabled": False,
            "strength": DEFAULT_STRENGTH,
            "previous_default_source": None,
            "pinned_target": None,
        }


def save_state(state):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def run(cmd, timeout=10):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)


def run_debug(cmd, timeout=10):
    """Like run(), but returns (ok, stdout, stderr) for diagnostics."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, "", str(e)


def notify(title, body):
    run(["notify-send", "-a", "Noise Cancel", title, body], timeout=3)


def get_default_source():
    ok, out = run(["pactl", "get-default-source"])
    return out if ok and out else None


def diagnose_no_source():
    """Build a debug string from raw command output so failures are
    self-explanatory instead of a bare 'no microphone found'."""
    pactl_path = shutil.which("pactl")
    ok1, out1, err1 = run_debug(["pactl", "get-default-source"])
    ok2, out2, err2 = run_debug(["pactl", "list", "short", "sources"])
    env_bits = [
        f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR', '<not set>')}",
        f"PULSE_SERVER={os.environ.get('PULSE_SERVER', '<not set>')}",
        f"DBUS_SESSION_BUS_ADDRESS={'<set>' if os.environ.get('DBUS_SESSION_BUS_ADDRESS') else '<not set>'}",
        f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '<not set>')}",
    ]
    lines = [
        f"pactl found: {pactl_path or 'NOT FOUND on PATH'}",
        f"pactl get-default-source -> ok={ok1} stdout='{out1}' stderr='{err1}'",
        f"pactl list short sources -> ok={ok2} stderr='{err2}'",
        out2 if out2 else "  (empty stdout)",
        "Environment:",
        *[f"  {b}" for b in env_bits],
    ]
    return "\n".join(lines)


def set_default_source(name):
    return run(["pactl", "set-default-source", name])


def list_sources():
    """Return [(name, state), ...] for real sources, excluding monitors and our own node."""
    ok, out = run(["pactl", "list", "sources"])
    sources = []
    if ok:
        name = None
        state = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("State:"):
                state = line.split(":", 1)[1].strip()
            elif line.startswith("Source #") and name is not None:
                sources.append((name, state))
                name, state = None, None
        if name is not None:
            sources.append((name, state))

    if not sources:
        ok2, out2 = run(["pactl", "list", "short", "sources"])
        if ok2:
            for line in out2.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    sources.append((parts[1], None))

    return [(n, s) for n, s in sources if n and not n.endswith(".monitor") and n != NODE_NAME]


def source_exists(name):
    ok, out = run(["pactl", "list", "short", "sources"])
    if not ok:
        return False
    return any(line.split("\t")[1] == name for line in out.splitlines() if "\t" in line)


def pick_best_real_source(prefer=None):
    """Pick a sensible physical input device: RUNNING > IDLE > SUSPENDED,
    preferring `prefer` if it's still present."""
    sources = list_sources()
    if not sources:
        return None
    if prefer:
        for n, _ in sources:
            if n == prefer:
                return prefer
    priority = {"RUNNING": 0, "IDLE": 1, "SUSPENDED": 2}
    sources.sort(key=lambda ns: priority.get(ns[1], 3))
    return sources[0][0]


# ---------------------------------------------------------------------------
# Standalone filter-chain config + its own systemd --user service
#
# This runs as an independent PipeWire client (like any normal app, e.g
# a browser), NOT embedded in the main daemon. Starting/stopping/restarting
# it only affects this one small process — never pipewire.service,
# pipewire-pulse.service, or wireplumber.service.
# ---------------------------------------------------------------------------

def _conf_escape(value):
    """Escape a value for safe embedding inside a double-quoted string in
    PipeWire's SPA-JSON config format. Device names come from hardware/OS
    metadata (e.g. a Bluetooth device's advertised name) and are not fully
    trusted input, so this is a defensive measure against a crafted name
    breaking out of the string and injecting config directives."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def write_filter_chain_conf(strength, target_object):
    plugin_path = read_plugin_path()
    if not plugin_path:
        raise RuntimeError("RNNoise plugin not found. Please run install.sh again.")
    if not target_object:
        raise RuntimeError("No microphone found to attach noise cancellation to.")

    plugin_path = _conf_escape(plugin_path)
    target_object = _conf_escape(target_object)

    os.makedirs(CONFIG_DIR, exist_ok=True)
    conf = f'''\
# Auto-generated by Noise Cancel — do not edit by hand.
# Run standalone with: pipewire -c this-file (handled by our systemd unit).
context.properties = {{ log.level = 2 }}
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*        = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rtkit
       args = {{ }}
       flags = [ ifexists nofail ]
    }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-filter-chain
       args = {{
            node.description = "{NODE_DESCRIPTION}"
            media.name = "{NODE_DESCRIPTION}"
            filter.graph = {{
                nodes = [
                    {{
                        type = ladspa
                        name = rnnoise
                        plugin = "{plugin_path}"
                        label = noise_suppressor_mono
                        control = {{
                            "VAD Threshold (%)" = {float(strength)}
                            "VAD Grace Period (ms)" = 200
                            "Retroactive VAD Grace (ms)" = 0
                        }}
                    }}
                ]
            }}
            capture.props = {{
                node.name = "capture.{NODE_NAME}"
                node.passive = true
                audio.rate = 48000
                audio.channels = 1
                target.object = "{target_object}"
            }}
            playback.props = {{
                node.name = "{NODE_NAME}"
                media.class = Audio/Source
                audio.rate = 48000
                audio.channels = 1
            }}
       }}
    }}
]
'''
    with open(FILTER_CONF_FILE, "w") as f:
        f.write(conf)


def write_systemd_service():
    os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
    pipewire_bin = shutil.which("pipewire") or "/usr/bin/pipewire"
    unit = f"""[Unit]
Description=Noise Cancel — standalone RNNoise PipeWire filter client
After=pipewire.service
Requisite=pipewire.service

[Service]
Type=simple
ExecStart={pipewire_bin} -c {FILTER_CONF_FILE}
Restart=on-failure
RestartSec=1
"""
    with open(SERVICE_FILE, "w") as f:
        f.write(unit)
    run(["systemctl", "--user", "daemon-reload"])


def service_action(action, timeout=15):
    return run(["systemctl", "--user", action, SERVICE_NAME], timeout=timeout)


def cleanup_legacy_pipewire_conf():
    """One-time migration: earlier versions of this app embedded the filter
    directly in the main PipeWire daemon's config, which required restarting
    pipewire/wireplumber to apply and could disrupt Bluetooth. If that file
    is still present, remove it and restart the main daemon ONCE to fully
    undo it. Safe to call every startup — it's a no-op once cleaned up."""
    if not os.path.isfile(LEGACY_PIPEWIRE_CONF):
        return False
    try:
        os.remove(LEGACY_PIPEWIRE_CONF)
    except OSError:
        return False
    run(["systemctl", "--user", "restart", "pipewire.service"], timeout=15)
    run(["systemctl", "--user", "restart", "wireplumber.service"], timeout=15)
    run(["systemctl", "--user", "restart", "pipewire-pulse.service"], timeout=15)
    return True


def diagnose_missing_filter_source():
    """Pull recent logs from OUR OWN standalone service (not the main
    pipewire daemon) so a failed filter load is self-explanatory."""
    ok, out = run(
        ["journalctl", "--user", "-u", SERVICE_NAME, "-n", "40", "--no-pager"], timeout=10
    )
    if not ok or not out:
        return f"(could not read logs — try `journalctl --user -u {SERVICE_NAME} -n 40` manually)"
    return out


def wait_for_source(name, attempts=30, delay=0.3):
    for _ in range(attempts):
        if source_exists(name):
            return True
        time.sleep(delay)
    return False


def set_autostart(enabled):
    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    if enabled:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        with open(AUTOSTART_FILE, "w") as f:
            f.write(f"""[Desktop Entry]
Type=Application
Name=Noise Cancel
Exec=python3 {os.path.join(app_dir, 'noise_cancel.py')} --autostart
X-GNOME-Autostart-enabled=true
NoDisplay=true
""")
    else:
        try:
            os.remove(AUTOSTART_FILE)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Dark theme (deliberately not dependent on the system GTK theme)
# ---------------------------------------------------------------------------

DARK_CSS = b"""
window, headerbar {
    background-color: #1b2430;
    color: #e8ecf1;
}
headerbar {
    border-bottom: 1px solid #0f151d;
    box-shadow: none;
}
headerbar label.title {
    color: #e8ecf1;
    font-weight: 600;
}
label { color: #e8ecf1; }
label.dim-label { color: #8b96a5; }
switch { background-color: #3a4657; border-radius: 999px; min-width: 44px; }
switch:checked { background-color: #4f8ef7; }
switch slider { background-color: #e8ecf1; border-radius: 999px; }
scale trough { background-color: #2a3441; border-radius: 6px; }
scale highlight { background-color: #4f8ef7; border-radius: 6px; }
scale slider { background-color: #e8ecf1; border-radius: 999px; }
expander title { color: #e8ecf1; }
expander arrow { color: #8b96a5; }
checkbutton check { background-color: #2a3441; border-color: #3a4657; }
checkbutton check:checked { background-color: #4f8ef7; }
button { background-color: #2a3441; color: #e8ecf1; border-color: #3a4657; }
button:hover { background-color: #33404f; }
tooltip { background-color: #0f151d; color: #e8ecf1; }
dialog, messagedialog { background-color: #1b2430; color: #e8ecf1; }
"""


def apply_dark_theme():
    settings = Gtk.Settings.get_default()
    if settings:
        settings.set_property("gtk-application-prefer-dark-theme", True)
    provider = Gtk.CssProvider()
    provider.load_from_data(DARK_CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class NoiseCancelWindow(Gtk.Window):
    RECOVERY_DEBOUNCE_SECONDS = 2.5
    MIN_RESTART_INTERVAL_SECONDS = 1.5  # our own service is cheap to restart

    def __init__(self, autostart_mode=False):
        super().__init__(title="Noise Cancel")
        self.set_default_size(380, -1)
        self.set_resizable(False)
        self.set_border_width(24)

        header = Gtk.HeaderBar(title="Noise Cancel")
        header.set_show_close_button(True)
        self.set_titlebar(header)

        cleanup_legacy_pipewire_conf()
        write_systemd_service()

        self.state = load_state()
        self.busy = False
        self.lock = threading.Lock()
        self.watch_proc = None
        self.watching = False
        self._debounce_timer = None
        self._last_restart_time = 0.0

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.add(outer)

        subtitle = Gtk.Label(
            label="Removes fan, keyboard, traffic and wind noise from your\n"
                  "mic in real time — works with built-in, USB, wired and\n"
                  "Bluetooth microphones."
        )
        subtitle.set_line_wrap(True)
        subtitle.set_halign(Gtk.Align.START)
        subtitle.get_style_context().add_class("dim-label")
        outer.pack_start(subtitle, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        outer.pack_start(row, False, False, 6)

        self.status_label = Gtk.Label(label="Noise cancellation")
        self.status_label.set_halign(Gtk.Align.START)
        row.pack_start(self.status_label, True, True, 0)

        self.switch = Gtk.Switch()
        self.switch.set_active(self.state.get("enabled", False))
        self.switch.set_valign(Gtk.Align.CENTER)
        self.switch.connect("state-set", self.on_switch_toggled)
        row.pack_start(self.switch, False, False, 0)

        self.spinner = Gtk.Spinner()
        row.pack_start(self.spinner, False, False, 0)

        self.detail_label = Gtk.Label(label="")
        self.detail_label.set_halign(Gtk.Align.START)
        self.detail_label.get_style_context().add_class("dim-label")
        outer.pack_start(self.detail_label, False, False, 0)

        expander = Gtk.Expander(label="Advanced")
        outer.pack_start(expander, False, False, 0)

        adv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        adv_box.set_margin_top(10)
        expander.add(adv_box)

        strength_label = Gtk.Label(label="Suppression strength")
        strength_label.set_halign(Gtk.Align.START)
        adv_box.pack_start(strength_label, False, False, 0)

        self.strength_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 30, 98, 1)
        self.strength_scale.set_value(self.state.get("strength", DEFAULT_STRENGTH))
        self.strength_scale.set_digits(0)
        self.strength_scale.connect("value-changed", self.on_strength_changed)
        adv_box.pack_start(self.strength_scale, False, False, 0)

        hint = Gtk.Label(label="Higher = more noise removed, but may clip quiet speech.")
        hint.set_halign(Gtk.Align.START)
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("dim-label")
        adv_box.pack_start(hint, False, False, 0)

        self.autostart_check = Gtk.CheckButton(label="Start automatically at login")
        self.autostart_check.set_active(os.path.isfile(AUTOSTART_FILE))
        self.autostart_check.connect("toggled", self.on_autostart_toggled)
        adv_box.pack_start(self.autostart_check, False, False, 4)

        self.connect("destroy", self.on_destroy)

        self._refresh_labels()

        if autostart_mode:
            self.set_visible(False)
            if self.state.get("enabled"):
                GLib.idle_add(self._toggle_worker, True)
        else:
            self.show_all()
            self.spinner.stop()
            self.spinner.hide()
            if self.state.get("enabled"):
                self._start_watch()

    # -- UI helpers ---------------------------------------------------
    def _refresh_labels(self):
        if self.state.get("enabled"):
            self.detail_label.set_text("Using: Noise Cancelled Microphone")
        else:
            self.detail_label.set_text("Using your regular microphone")

    def _set_busy(self, busy, message=""):
        self.busy = busy
        self.switch.set_sensitive(not busy)
        self.strength_scale.set_sensitive(not busy)
        if busy:
            self.spinner.show()
            self.spinner.start()
            self.detail_label.set_text(message)
        else:
            self.spinner.stop()
            self.spinner.hide()
            self._refresh_labels()

    # -- Toggle handling -------------------------------------------------
    def on_switch_toggled(self, switch, gtk_state):
        if self.busy:
            return True
        threading.Thread(target=self._toggle_worker, args=(gtk_state,), daemon=True).start()
        return True

    def _toggle_worker(self, enable):
        with self.lock:
            GLib.idle_add(self._set_busy, True, "Enabling…" if enable else "Disabling…")
            try:
                if enable:
                    self._apply_enable()
                else:
                    self._stop_watch()
                    self._apply_disable()
            finally:
                GLib.idle_add(self._finish_toggle, enable)

    def _finish_toggle(self, enable):
        self._set_busy(False)
        self.switch.set_state(self.state.get("enabled", False))
        self.switch.set_active(self.state.get("enabled", False))

    def _safe_restart_service(self):
        """(Re)start our own standalone filter service, with a small
        minimum interval between restarts so rapid-fire triggers can't
        stack. This only ever affects our own tiny process — never the
        main PipeWire daemon, WirePlumber, or Bluetooth."""
        elapsed = time.time() - self._last_restart_time
        if elapsed < self.MIN_RESTART_INTERVAL_SECONDS:
            time.sleep(self.MIN_RESTART_INTERVAL_SECONDS - elapsed)
        service_action("restart")
        self._last_restart_time = time.time()

    def _apply_enable(self):
        try:
            current = get_default_source()
            target = current if (current and current != NODE_NAME) else pick_best_real_source()
            if not target:
                raise RuntimeError(
                    "No microphone was found to enable noise cancellation on.\n\n"
                    "Debug info:\n" + diagnose_no_source()
                )

            if current and current != NODE_NAME:
                self.state["previous_default_source"] = current
            self.state["pinned_target"] = target

            strength = self.strength_scale.get_value()
            write_filter_chain_conf(strength, target)
            self._safe_restart_service()
            if not wait_for_source(NODE_NAME):
                raise RuntimeError(
                    "The noise-cancelled microphone didn't appear.\n\n"
                    "Service log (most recent lines):\n" + diagnose_missing_filter_source()
                )
            set_default_source(NODE_NAME)
            self.state["enabled"] = True
            self.state["strength"] = strength
            save_state(self.state)
            self._start_watch()
        except Exception as e:
            GLib.idle_add(self._show_error, str(e))
            self.state["enabled"] = False
            save_state(self.state)

    def _apply_disable(self, restore=True):
        try:
            prev = self.state.get("previous_default_source")
            service_action("stop")
            if restore and prev and prev != NODE_NAME:
                set_default_source(prev)
            self.state["enabled"] = False
            save_state(self.state)
        except Exception as e:
            GLib.idle_add(self._show_error, str(e))

    def on_strength_changed(self, scale):
        self.state["strength"] = scale.get_value()
        save_state(self.state)
        if self.state.get("enabled") and not self.busy:
            threading.Thread(target=self._reapply, daemon=True).start()

    def _reapply(self):
        with self.lock:
            GLib.idle_add(self._set_busy, True, "Updating…")
            try:
                target = self.state.get("pinned_target") or pick_best_real_source()
                write_filter_chain_conf(self.strength_scale.get_value(), target)
                self._safe_restart_service()
                wait_for_source(NODE_NAME)
                set_default_source(NODE_NAME)
            except Exception as e:
                GLib.idle_add(self._show_error, str(e))
            finally:
                GLib.idle_add(self._set_busy, False)

    def on_autostart_toggled(self, check):
        set_autostart(check.get_active())

    def _show_error(self, message):
        dialog = Gtk.MessageDialog(
            transient_for=self, flags=0, message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK, text="Noise Cancel ran into a problem",
        )
        dialog.format_secondary_text(message)
        for widget in dialog.get_message_area().get_children():
            if isinstance(widget, Gtk.Label):
                widget.set_selectable(True)
        dialog.run()
        dialog.destroy()

    # -- Device disconnect / reconnect watcher --------------------------
    # Falls back to another available microphone if the pinned one drops
    # out mid-session (e.g. Bluetooth headset going out of range), instead
    # of silently going quiet. Debounced so a Bluetooth profile switch's
    # rapid flicker of events doesn't trigger repeated action.
    def _start_watch(self):
        if self.watching:
            return
        self.watching = True
        threading.Thread(target=self._watch_loop, daemon=True).start()

    def _stop_watch(self):
        self.watching = False
        if self._debounce_timer:
            self._debounce_timer.cancel()
            self._debounce_timer = None
        if self.watch_proc:
            try:
                self.watch_proc.terminate()
            except Exception:
                pass
            self.watch_proc = None

    def _watch_loop(self):
        try:
            self.watch_proc = subprocess.Popen(
                ["pactl", "subscribe"], stdout=subprocess.PIPE, text=True,
            )
        except FileNotFoundError:
            self.watching = False
            return

        for line in self.watch_proc.stdout:
            if not self.watching:
                break
            if "source" not in line:
                continue
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                self.RECOVERY_DEBOUNCE_SECONDS, self._debounced_check
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()
        self.watching = False

    def _debounced_check(self):
        pinned = self.state.get("pinned_target")
        if pinned and self.state.get("enabled") and not source_exists(pinned):
            GLib.idle_add(self._handle_device_lost)

    def _handle_device_lost(self):
        if self.lock.locked():
            return
        threading.Thread(target=self._recover_worker, daemon=True).start()

    def _recover_worker(self):
        with self.lock:
            pinned = self.state.get("pinned_target")
            if not self.state.get("enabled") or (pinned and source_exists(pinned)):
                return
            GLib.idle_add(self._set_busy, True, "Microphone disconnected, recovering…")
            fallback = pick_best_real_source()
            try:
                if fallback:
                    self.state["pinned_target"] = fallback
                    write_filter_chain_conf(self.strength_scale.get_value(), fallback)
                    self._safe_restart_service()
                    wait_for_source(NODE_NAME)
                    set_default_source(NODE_NAME)
                    save_state(self.state)
                    GLib.idle_add(
                        notify, "Noise Cancel",
                        "Microphone changed — switched noise cancellation to the new device.",
                    )
                else:
                    self._apply_disable(restore=False)
                    GLib.idle_add(
                        notify, "Noise Cancel",
                        "No microphone available — noise cancellation turned off.",
                    )
            except Exception as e:
                GLib.idle_add(self._show_error, str(e))
            finally:
                GLib.idle_add(self._finish_toggle, self.state.get("enabled", False))
                GLib.idle_add(self._set_busy, False)

    def on_destroy(self, *args):
        self._stop_watch()
        Gtk.main_quit()


def main():
    apply_dark_theme()
    autostart_mode = "--autostart" in sys.argv
    win = NoiseCancelWindow(autostart_mode=autostart_mode)
    if autostart_mode and not win.state.get("enabled"):
        return
    Gtk.main()


if __name__ == "__main__":
    main()
