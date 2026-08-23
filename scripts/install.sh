#!/usr/bin/env bash
# Install the Omarchy Sonos bar plugin and the PipeWire AirPlay pieces it needs.
set -euo pipefail

root=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
plugin_id="tbye.sonos"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
plugin_dst="$config_home/omarchy/plugins/$plugin_id"

have() { command -v "$1" >/dev/null 2>&1; }

echo "==> PipeWire AirPlay (RAOP) discovery"
if have omarchy && omarchy pkg missing pipewire-zeroconf >/dev/null 2>&1; then
  omarchy pkg add pipewire-zeroconf
elif have pacman; then
  sudo pacman -S --needed --noconfirm pipewire-zeroconf
else
  echo "    Install pipewire-zeroconf with your package manager, then re-run."
fi

mkdir -p "$config_home/pipewire/pipewire.conf.d"
cp "$root/contrib/raop-discover.conf" "$config_home/pipewire/pipewire.conf.d/raop-discover.conf"
echo "    Wrote $config_home/pipewire/pipewire.conf.d/raop-discover.conf"

echo "==> Plugin"
mkdir -p "$(dirname "$plugin_dst")"
if [ -e "$plugin_dst" ] && [ ! -L "$plugin_dst" ]; then
  bak="$plugin_dst.bak.$(date +%s)"
  echo "    Moving existing plugin to $bak"
  mv "$plugin_dst" "$bak"
fi
ln -sfn "$root" "$plugin_dst"
echo "    $plugin_dst -> $root"

echo "==> CLI"
mkdir -p "$HOME/.local/bin"
ln -sfn "$root/bin/omarchy-sonos" "$HOME/.local/bin/omarchy-sonos"
echo "    ~/.local/bin/omarchy-sonos"

if have omarchy; then
  echo "==> Enable in the Omarchy bar"
  omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
  omarchy plugin enable "$plugin_id" --before omarchy.audio >/dev/null 2>&1 \
    || omarchy plugin enable "$plugin_id" >/dev/null 2>&1 \
    || true
  omarchy restart audio || true
  omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
fi

echo
echo "Done. Click the wireless-speaker icon in the bar."
echo "If speakers appear but audio will not route, allow UDP 6001-6010 from your LAN:"
echo "  sudo ufw allow from 192.168.0.0/16 to any port 6001:6010 proto udp comment 'sonos-raop'"
echo "AirPlay to Sonos has ~1.5s of latency; that is expected."
