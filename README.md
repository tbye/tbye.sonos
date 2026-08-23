# omarchy-sonos

Omarchy bar widget that sends this computer's audio to a Sonos speaker and
controls volume on every speaker in the household.

Click the wireless-speaker icon (just left of the volume control). Pick
**This computer** or any room. Each Sonos row has its own volume slider.

System audio is routed over AirPlay (PipeWire RAOP). Volume uses the same
UPnP calls as the Sonos app, so you can turn any speaker up or down even
when it is not the current output.

## Requirements

- [Omarchy](https://omarchy.org/) (Hyprland + Omarchy shell / Quickshell)
- Sonos speakers on the same LAN, with AirPlay enabled
- `pipewire-zeroconf` (provides `libpipewire-module-raop-discover`)
- Python 3 (stdlib only)

## Install

From this repo:

```bash
./scripts/install.sh
```

That installs PipeWire AirPlay discovery, links this checkout into
`~/.config/omarchy/plugins/tbye.sonos`, puts `omarchy-sonos` on `PATH`,
and enables the widget next to the stock audio control.

Or, on a machine that already has Omarchy:

```bash
omarchy plugin add https://github.com/tbye/omarchy-sonos.git --enable --yes
omarchy bar move tbye.sonos --before omarchy.audio
# still run the RAOP half of install.sh so speakers show up as audio sinks
./scripts/install.sh
```

If speakers appear in the list but play stays on the laptop, open UDP
6001–6010 from your LAN (AirPlay timing/control):

```bash
sudo ufw allow from 192.168.0.0/16 to any port 6001:6010 proto udp comment 'sonos-raop'
```

## Usage

- Left-click the bar icon: open the picker
- Click a speaker name: send system audio there
- Drag the slider under a name: set that speaker's volume
- Right-click the bar icon: switch back to this computer
- Scroll-wheel on the icon: volume of the current output

AirPlay to Sonos has about 1.5 seconds of latency. Fine for music; not
great for games or video unless you can delay the picture to match.

## CLI

```bash
omarchy-sonos list
omarchy-sonos status
omarchy-sonos set-output local
omarchy-sonos set-output RINCON_...
omarchy-sonos volume RINCON_... 25
omarchy-sonos mute RINCON_... toggle
```

`list` prints JSON with rooms, volume, mute, and whether the matching
AirPlay sink is ready.

## How it works

1. **Discovery and volume** — `sonosctl.py` finds the household through
   a Sonos `GetZoneGroupState` query (seeded from Avahi `_sonos._tcp`)
   and talks RenderingControl SOAP for volume/mute. Invisible stereo-pair
   partners and surround satellites that share a room name are folded
   into that room; a distinctly named Sub still gets its own slider.
2. **Output switching** — PipeWire RAOP creates a sink per AirPlay
   receiver. The widget matches sinks by the speaker's MAC (`Sonos-<MAC>`
   in the sink name) so a room like "Basement" is not confused with an
   Apple TV of the same name. Selecting a room calls
   `omarchy-audio-output-set-default`.

State lives in `~/.local/state/omarchy-sonos/`, not in the plugin
directory, so saving it does not reload Omarchy shell.

## Issues, support and feedback welcome

Please create an [issue](https://github.com/tbye/omarchy-sonos/issues/new)
if there's anything I can help you with.
