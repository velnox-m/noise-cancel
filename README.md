# Noise Cancel — simple mic noise removal for Fedora (Workstation) Linux

A tiny, one-toggle app that strips fan noise, keyboard clicks, traffic, wind,
and background chatter out of your microphone in real time — without
touching the words you actually say. Built specifically for Fedora
Workstation's default audio stack (PipeWire), in the spirit of NoiseTorch:
**one switch, nothing else to configure**, unlike Easy Effects' wall of dials.

Works with built-in laptop mics, USB microphones, wired headset mics, and
Bluetooth headsets (HSP/HFP) — whatever is your active input device when you
flip the switch gets used, and it's dark-themed regardless of your system
theme so it isn't a wall of white at night.

Under the hood it uses [RNNoise](https://github.com/xiph/rnnoise), the same
neural-network noise suppressor NoiseTorch, EasyEffects and several other
tools are built on — loaded as a native PipeWire filter, not a separate
audio server hack.

## Install

```bash
chmod +x install.sh
./install.sh
```

You'll be asked for your password once, to install a couple of small system
packages (`python3-gobject`, `gtk3`, `pipewire-utils`) if they're not already
present — most Fedora Workstation installs already have them. The noise
suppression plugin itself installs into your home directory, no root needed.

Then launch **Noise Cancel** from your app grid, or run `noisecancel-fedora`
in a terminal.

## Use

- Flip the switch. That's it.
- Under **Advanced** there's one slider (how aggressively noise is removed)
  and a "start at login" checkbox. Everything else is deliberately hidden —
  you shouldn't need a manual to stop your fan from being heard on a call.
- Any app that reads from your microphone (browser, Discord, Zoom, OBS,
  Meet) will now hear the cleaned-up audio automatically, because the app
  switches your system default microphone for you. Flip the switch off and
  you're back to your normal mic instantly.

## How it works (if you're curious)

1. Runs RNNoise as its own small, standalone PipeWire client process —
   started via a dedicated `~/.config/systemd/user/noisecancel-fedora.service`
   unit — that connects to your existing PipeWire session the same way any
   normal app does (like a browser or a media player). It is deliberately
   **not** embedded into the main PipeWire daemon's own config.
2. That process publishes a new virtual microphone, "Noise Cancelled
   Microphone", by running your real mic's audio through the RNNoise filter.
3. Points your system default microphone at it with `pactl set-default-source`.
4. Turning the switch off just stops that one small process and restores
   whatever microphone was your default before.

Because the filter runs as a separate client rather than inside the main
daemon, turning it on/off/adjusting strength **never restarts PipeWire,
PipeWire-Pulse, or WirePlumber** — so it can't cause the brief audio blip
those restarts used to cause, and (importantly) can't disrupt Bluetooth
audio, which WirePlumber manages. Only starting/stopping our own tiny
process is involved.

Nothing runs in the background recording or uploading audio anywhere — it's
a local PipeWire graph, same category of thing as a hardware EQ pedal.

## Headphones and Bluetooth

The filter is pinned to whichever real input device is your system default
*at the moment you flip the switch* — not to "whatever the default is" in a
loose, ever-shifting sense, which matters because once the noise-cancelled
mic itself becomes the default, a filter that just followed "the default"
blindly would end up trying to listen to itself.

If that pinned device disappears mid-session (Bluetooth headset goes out of
range, USB mic unplugged), Noise Cancel waits a couple of seconds for
things to settle (Bluetooth profile switches briefly flicker devices
in and out), then automatically switches to the next available microphone
and shows a desktop notification telling you it happened. If no microphone
is left at all, it turns itself off cleanly and tells you why.

One honest limitation: Bluetooth headsets using the HSP/HFP call profile
have inherently low audio bandwidth (often 8–16 kHz, narrower than a wired
mic) — that's a limit of the Bluetooth audio codec itself, not something any
software noise filter can fix. RNNoise still removes background noise fine
on that signal; it just can't add back detail the codec never captured.

## Troubleshooting

**Switch turns on but nothing seems to change / error about the mic not appearing**
Run `pactl list short sources` — you should see `noisecancel_fedora_mic` in
the list within a couple of seconds of flipping the switch. If it's missing:
- Confirm the plugin file exists: `ls ~/.local/lib/ladspa/librnnoise_ladspa.so`
- Check our service's own logs (not the main PipeWire daemon's):
  `journalctl --user -u noisecancel-fedora.service -n 50 --no-pager`
- Check the service status: `systemctl --user status noisecancel-fedora.service`
- Re-run `./install.sh` — it's safe to run again.

**I use a specific USB mic and it's not being picked up**
The filter automatically follows whatever your *current* default microphone
is at the moment you flip the switch — set your preferred mic as default
first (Settings → Sound → Input), then enable Noise Cancel.

**Video call app "forgot" my noise-cancelled mic after I reconnected**
Some apps cache the device list at launch. Restart the call app if you
toggle Noise Cancel mid-session, or (recommended) turn it on before joining.

## Security & trust

Worth knowing before you run `install.sh`:
- The only step needing `sudo` installs a couple of small, standard Fedora
  packages (`python3-gobject`, `gtk3`, `pipewire-utils`).
- The RNNoise plugin binary is downloaded from a **pinned release** of
  [werman/noise-suppression-for-voice](https://github.com/werman/noise-suppression-for-voice)
  and its SHA-256 checksum is verified before it's installed — if the
  download doesn't match, install.sh refuses and changes nothing.
- Everything else lives under your home directory (`~/.local`, `~/.config`).
  Nothing is installed system-wide, nothing listens on a network port,
  nothing phones home.
- The app runs as a normal user process; it doesn't need or request root
  at runtime.

## Uninstall

```bash
chmod +x uninstall.sh
./uninstall.sh
```

Restores your normal microphone and removes all installed files.
