#!/usr/bin/env bash
# Remove session extras installed by scripts/install.sh.
# Does not delete the plugin checkout; run `omarchy plugin remove tbye.sonos`
# after this, as Omarchy has no uninstall hooks.
set -euo pipefail

plugin_id="tbye.sonos"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
raop="$config_home/pipewire/pipewire.conf.d/raop-discover.conf"
cli="$HOME/.local/bin/omarchy-sonos"
state_dir="$state_home/omarchy-sonos"
plugins_dir="$config_home/omarchy/plugins"

have() { command -v "$1" >/dev/null 2>&1; }

echo "==> PipeWire RAOP drop-in"
if [ -f "$raop" ] && grep -q 'managed-by: tbye.sonos' "$raop"; then
  rm -f "$raop"
  echo "    Removed $raop"
elif [ -f "$raop" ]; then
  echo "    Left $raop (not tagged managed-by: tbye.sonos)"
else
  echo "    Not present"
fi

echo "==> CLI"
if [ -L "$cli" ]; then
  target=$(readlink -f "$cli" 2>/dev/null || true)
  case "$target" in
    */tbye.sonos/bin/omarchy-sonos|*/bin/omarchy-sonos)
      rm -f "$cli"
      echo "    Removed $cli"
      ;;
    *)
      echo "    Left $cli (target is $target)"
      ;;
  esac
elif [ -e "$cli" ]; then
  echo "    Left $cli (not a symlink)"
else
  echo "    Not present"
fi

echo "==> State"
if [ -d "$state_dir" ]; then
  rm -rf "$state_dir"
  echo "    Removed $state_dir"
else
  echo "    Not present"
fi

echo "==> Leftover installer backups"
shopt -s nullglob
baks=("$plugins_dir/$plugin_id.bak."*)
if [ ${#baks[@]} -gt 0 ]; then
  rm -rf "${baks[@]}"
  echo "    Removed ${baks[*]}"
else
  echo "    None"
fi
shopt -u nullglob

if have omarchy; then
  echo "==> Reload audio"
  omarchy restart audio || true
fi

echo
echo "Session extras removed. To remove the plugin itself:"
echo "  omarchy plugin remove $plugin_id"
echo "pipewire-zeroconf is left installed in case something else uses it."
