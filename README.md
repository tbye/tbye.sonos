# tbye.sonos

Omarchy bar widget that sends this computer's audio to a Sonos speaker and
controls volume on every speaker in the household.

Click the wireless-speaker icon (just left of the volume control). Pick
**This computer** or any room. Each Sonos row has its own volume slider.

System audio is routed over AirPlay (PipeWire RAOP). Volume uses the same
UPnP calls as the Sonos app, so you can turn any speaker up or down even
when it is not the current output. Switching to a room reads that
speaker's current volume first and applies it to the AirPlay sink, so
playback does not jump to 100%. While a room is the current output, the
system volume and that speaker stay in sync — volume keys, the audio
panel, and the Sonos slider all drive the same level.

This is a `bar-widget`. The details panel is part of that widget; the
plugin does not start a second Quickshell process.

## Install

Needs Omarchy, Python 3 (standard library only), Sonos speakers on the
same LAN with AirPlay enabled, and `pipewire-zeroconf`.

```sh
omarchy plugin add https://github.com/tbye/tbye.sonos.git --enable
omarchy bar move tbye.sonos --before omarchy.audio
```

AirPlay output needs PipeWire RAOP discovery. Omarchy never runs plugin
install hooks, so that half is a separate user-level script:

```sh
~/.config/omarchy/plugins/tbye.sonos/scripts/install.sh
```

`install.sh` may install the `pipewire-zeroconf` package (`omarchy pkg
add`; if that is unavailable it prints a `pacman` command). It writes a
user PipeWire drop-in tagged `managed-by: tbye.sonos` and links
`omarchy-sonos` into `~/.local/bin`. It does not move, replace, or
symlink the plugin checkout — Omarchy rejects plugin folders that contain
symlinks.

## Usage

- Left-click the bar icon: open the picker
- Click a speaker name: send system audio there
- Drag the slider under a name: set that speaker's volume
- Right-click the bar icon: switch back to this computer
- Scroll-wheel on the icon: volume of the current output
- Volume keys / the system audio panel: same level on the current Sonos room
- Escape closes the panel

AirPlay to Sonos has about 1.5 seconds of latency. Fine for music; not
great for games or video unless you can delay the picture to match.

## Configure

```sh
omarchy bar move tbye.sonos --before omarchy.audio
```

If speakers appear in the list but play stays on the laptop, open UDP
6001–6010 from your LAN (AirPlay timing/control). That is a firewall
change, not something the plugin runs for you:

```sh
sudo ufw allow from 192.168.0.0/16 to any port 6001:6010 proto udp comment 'sonos-raop'
```

## CLI

```sh
omarchy-sonos list
omarchy-sonos status
omarchy-sonos set-output local
omarchy-sonos set-output RINCON_...
omarchy-sonos volume RINCON_... 25
omarchy-sonos mute RINCON_... toggle
```

`list` prints JSON with rooms, volume, mute, and whether the matching
AirPlay sink is ready.

## Remove

Omarchy does not run uninstall hooks. Remove session extras first, then
the plugin:

```sh
~/.config/omarchy/plugins/tbye.sonos/scripts/uninstall.sh
omarchy plugin remove tbye.sonos
```

`uninstall.sh` removes the RAOP drop-in, the `omarchy-sonos` CLI link,
`~/.local/state/omarchy-sonos`, and leftover `tbye.sonos.bak.*` copies
from older installers. It does not remove `pipewire-zeroconf`.

## How it works

1. **Discovery and volume** — `sonosctl.py` finds the household through
   a Sonos `GetZoneGroupState` query (seeded from Avahi `_sonos._tcp`)
   and talks RenderingControl SOAP for volume/mute. Invisible stereo-pair
   partners and surround satellites that share a room name are folded
   into that room; a distinctly named Sub still gets its own slider.
   Avahi/SOAP payloads are byte-capped, topology is capped at 32 rooms,
   names are stripped of markup, and a lookup is aborted after 8 seconds
   so a LAN responder cannot retain the helper or the shared shell.
2. **Output switching** — PipeWire RAOP creates a sink per AirPlay
   receiver. The widget matches sinks by the speaker's MAC (`Sonos-<MAC>`
   in the sink name) so a room like "Basement" is not confused with an
   Apple TV of the same name. Selecting a room calls
   `omarchy-audio-output-set-default` after setting the sink volume to
   the speaker's current level.

State lives in `~/.local/state/omarchy-sonos/`, not in the plugin
directory, so saving it does not reload Omarchy shell. The helper
byte-caps that file, opens it without following a symlink, and writes
through an exclusive temporary name so a planted entry cannot retain
the shared shell or overwrite another path.

## Issues, support and feedback welcome

Please create an [issue](https://github.com/tbye/tbye.sonos/issues/new)
if there's anything I can help you with.
