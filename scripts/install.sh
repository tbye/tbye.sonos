#!/usr/bin/env bash
# Install session extras this plugin needs (PipeWire RAOP + optional CLI).
# Does not move or symlink the plugin folder: `omarchy plugin add` owns that,
# and Omarchy rejects plugin directories that contain symlinks.
set -euo pipefail

root=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
plugin_id="tbye.sonos"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"

have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<EOF
Usage: $(basename "$0") [--runtime]

Install PipeWire AirPlay discovery and the omarchy-sonos CLI. This script
never relocates ~/.config/omarchy/plugins/${plugin_id}.

  --runtime   Same as the default (accepted for compatibility).
  -h, --help  Show this help.
EOF
}

case "${1:-}" in
  ""|--runtime) ;;
  -h|--help) usage; exit 0 ;;
  *)
    echo "Unknown option: $1" >&2
    usage >&2
    exit 2
    ;;
esac

echo "==> PipeWire AirPlay (RAOP) discovery"
if have omarchy && omarchy pkg missing pipewire-zeroconf >/dev/null 2>&1; then
  omarchy pkg add pipewire-zeroconf
elif have pacman && ! pacman -Q pipewire-zeroconf >/dev/null 2>&1; then
  echo "    Missing pipewire-zeroconf. Install it, then re-run:"
  echo "      sudo pacman -S --needed pipewire-zeroconf"
fi

mkdir -p "$config_home/pipewire/pipewire.conf.d"
cp "$root/contrib/raop-discover.conf" "$config_home/pipewire/pipewire.conf.d/raop-discover.conf"
echo "    Wrote $config_home/pipewire/pipewire.conf.d/raop-discover.conf"

echo "==> CLI"
mkdir -p "$HOME/.local/bin"
ln -sfn "$root/bin/omarchy-sonos" "$HOME/.local/bin/omarchy-sonos"
echo "    $HOME/.local/bin/omarchy-sonos -> $root/bin/omarchy-sonos"

if have omarchy; then
  echo "==> Reload audio"
  omarchy restart audio || true
fi

echo
echo "Done. Click the wireless-speaker icon in the bar."
echo "State lives in $state_home/omarchy-sonos/."
echo "If speakers appear but audio will not route, allow UDP 6001-6010 from your LAN:"
echo "  sudo ufw allow from 192.168.0.0/16 to any port 6001:6010 proto udp comment 'sonos-raop'"
echo "AirPlay to Sonos has ~1.5s of latency; that is expected."
echo
echo "This script does not install the plugin itself. Use:"
echo "  omarchy plugin add https://github.com/tbye/tbye.sonos.git --enable"
echo "Remove extras later with $root/scripts/uninstall.sh"
