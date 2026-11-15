#!/bin/sh
# Add Brev to your app grid, running from this source checkout.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
mkdir -p "$data/applications" "$data/icons/hicolor/scalable/apps" "$data/icons/hicolor/symbolic/apps" "$HOME/.local/bin"
ln -sf "$here/bin/brev" "$HOME/.local/bin/brev"
sed "s|^Exec=brev|Exec=$here/bin/brev|" "$here/data/se.croby.Brev.desktop" > "$data/applications/se.croby.Brev.desktop"
cp "$here/data/icons/hicolor/scalable/apps/se.croby.Brev.svg" "$data/icons/hicolor/scalable/apps/"
cp "$here/data/icons/hicolor/symbolic/apps/se.croby.Brev-symbolic.svg" "$data/icons/hicolor/symbolic/apps/"
update-desktop-database "$data/applications" 2>/dev/null || true
gtk4-update-icon-cache -q -t "$data/icons/hicolor" 2>/dev/null || true
echo "Installed. Brev is now in your app grid."
