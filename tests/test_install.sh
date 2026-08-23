#!/usr/bin/env bash
# Installer must never replace a plugin checkout with a symlink.
set -euo pipefail

root=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

export HOME="$tmp/home"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_STATE_HOME="$HOME/.local/state"
mkdir -p "$HOME/.config/omarchy/plugins" "$HOME/.local/bin" "$tmp/bin"

cat > "$tmp/bin/omarchy" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$tmp/bin/omarchy"
export PATH="$tmp/bin:$PATH"

plugin_dst="$HOME/.config/omarchy/plugins/tbye.sonos"
cp -a "$root" "$plugin_dst"
# Drop the live git dir so the copy looks like an omarchy plugin add checkout.
rm -rf "$plugin_dst/.git"

marker="$plugin_dst/Panel.qml"
test -f "$marker"

"$plugin_dst/scripts/install.sh" --runtime >/dev/null

if [ -L "$plugin_dst" ]; then
  echo "FAIL: install.sh turned the plugin checkout into a symlink" >&2
  ls -ld "$plugin_dst" >&2
  exit 1
fi
if [ ! -f "$marker" ]; then
  echo "FAIL: plugin files missing after install" >&2
  exit 1
fi
if [ ! -f "$HOME/.config/pipewire/pipewire.conf.d/raop-discover.conf" ]; then
  echo "FAIL: RAOP drop-in was not written" >&2
  exit 1
fi
if ! grep -q 'managed-by: tbye.sonos' "$HOME/.config/pipewire/pipewire.conf.d/raop-discover.conf"; then
  echo "FAIL: RAOP drop-in is missing managed-by tag" >&2
  exit 1
fi
if [ ! -L "$HOME/.local/bin/omarchy-sonos" ]; then
  echo "FAIL: CLI link was not created" >&2
  exit 1
fi

# A leftover backup from the old installer should be removed on uninstall.
mkdir -p "$HOME/.config/omarchy/plugins/tbye.sonos.bak.1/keep"
"$plugin_dst/scripts/uninstall.sh" >/dev/null

if [ -e "$HOME/.config/pipewire/pipewire.conf.d/raop-discover.conf" ]; then
  echo "FAIL: uninstall left the RAOP drop-in" >&2
  exit 1
fi
if [ -e "$HOME/.local/bin/omarchy-sonos" ]; then
  echo "FAIL: uninstall left the CLI link" >&2
  exit 1
fi
if [ -e "$HOME/.config/omarchy/plugins/tbye.sonos.bak.1" ]; then
  echo "FAIL: uninstall left installer backup copies" >&2
  exit 1
fi
if [ ! -f "$marker" ]; then
  echo "FAIL: uninstall removed the plugin checkout" >&2
  exit 1
fi

echo "test_install.sh ok"
