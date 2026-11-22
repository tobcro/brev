#!/bin/sh
# Add Brev to your app grid, running from this source checkout.
#
# First checks that Python can load everything Brev needs (PyGObject, GTK 4,
# libadwaita, GNOME Online Accounts, WebKitGTK 6) and installs whatever is
# missing with your distribution's package manager. That step asks for your
# password through sudo. Skip it with --no-deps.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"

install_deps=yes
for arg in "$@"; do
    case "$arg" in
        --no-deps) install_deps=no ;;
        -h|--help)
            echo "Usage: ./install.sh [--no-deps]"
            echo "  --no-deps  don't check for or install the libraries Brev needs"
            exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# --- Requirements ------------------------------------------------------------

# Prints the missing pieces as words: python, gi, Gtk, Adw, Goa, WebKit.
missing_requirements() {
    if ! command -v python3 >/dev/null 2>&1; then
        echo python gi Gtk Adw Goa WebKit
        return
    fi
    python3 - <<'EOF'
import sys
try:
    import gi
except ImportError:
    print("gi Gtk Adw Goa WebKit")
    sys.exit()
missing = []
for name, version in (("Gtk", "4.0"), ("Adw", "1"), ("Goa", "1.0"), ("WebKit", "6.0")):
    try:
        gi.require_version(name, version)
        __import__("gi.repository." + name)
    except (ValueError, ImportError):
        missing.append(name)
print(" ".join(missing))
EOF
}

# The distribution family, from /etc/os-release (ID, then ID_LIKE), or failing
# that from whichever package manager is around.
distro_family() {
    ids=""
    if [ -r /etc/os-release ]; then
        ids="$(. /etc/os-release; echo "${ID:-} ${ID_LIKE:-}")"
    fi
    for id in $ids; do
        case "$id" in
            debian|ubuntu|linuxmint|pop) echo debian; return ;;
            fedora|rhel|centos) echo fedora; return ;;
            arch|archlinux|manjaro|endeavouros) echo arch; return ;;
            opensuse*|suse|sles) echo suse; return ;;
        esac
    done
    for tool in apt-get:debian dnf:fedora pacman:arch zypper:suse; do
        if command -v "${tool%%:*}" >/dev/null 2>&1; then
            echo "${tool#*:}"
            return
        fi
    done
    echo unknown
}

# The package that provides one missing piece on one distribution family.
package_for() {  # family piece
    case "$1:$2" in
        debian:python) echo python3 ;;
        debian:gi)     echo python3-gi ;;
        debian:Gtk)    echo gir1.2-gtk-4.0 ;;
        debian:Adw)    echo gir1.2-adw-1 ;;
        debian:Goa)    echo gir1.2-goa-1.0 ;;
        debian:WebKit) echo gir1.2-webkit-6.0 ;;

        fedora:python) echo python3 ;;
        fedora:gi)     echo python3-gobject ;;
        fedora:Gtk)    echo gtk4 ;;
        fedora:Adw)    echo libadwaita ;;
        fedora:Goa)    echo gnome-online-accounts-libs ;;
        fedora:WebKit) echo webkitgtk6.0 ;;

        arch:python)   echo python ;;
        arch:gi)       echo python-gobject ;;
        arch:Gtk)      echo gtk4 ;;
        arch:Adw)      echo libadwaita ;;
        arch:Goa)      echo libgoa ;;
        arch:WebKit)   echo webkitgtk-6.0 ;;

        suse:python)   echo python3 ;;
        suse:gi)       echo python3-gobject ;;
        suse:Gtk)      echo typelib-1_0-Gtk-4_0 ;;
        suse:Adw)      echo typelib-1_0-Adw-1 ;;
        suse:Goa)      echo typelib-1_0-Goa-1_0 ;;
        suse:WebKit)   echo typelib-1_0-WebKit-6_0 ;;
    esac
}

install_packages() {  # family packages...
    family="$1"; shift
    sudo=""
    if [ "$(id -u)" -ne 0 ]; then
        if ! command -v sudo >/dev/null 2>&1; then
            echo "sudo is not available. As root, install: $*" >&2
            return 1
        fi
        sudo=sudo
    fi
    case "$family" in
        debian) $sudo apt-get update && $sudo apt-get install -y "$@" ;;
        fedora) $sudo dnf install -y "$@" ;;
        arch)   $sudo pacman -S --needed --noconfirm "$@" ;;
        suse)   $sudo zypper --non-interactive install "$@" ;;
    esac
}

if [ "$install_deps" = yes ]; then
    missing="$(missing_requirements)"
    if [ -z "$missing" ]; then
        echo "Everything Brev needs is already installed."
    else
        family="$(distro_family)"
        packages=""
        for piece in $missing; do
            packages="$packages $(package_for "$family" "$piece")"
        done
        packages="$(echo $packages)"  # tidy the spaces
        if [ "$family" = unknown ] || [ -z "$packages" ]; then
            echo "Brev needs these, but this script doesn't know your distribution's package names:" >&2
            echo "  Python 3, PyGObject, and the GObject introspection data for GTK 4," >&2
            echo "  libadwaita, GNOME Online Accounts and WebKitGTK 6." >&2
            echo "Install them with your package manager, then run ./install.sh again." >&2
            exit 1
        fi
        echo "Brev needs a few more packages: $packages"
        echo "Installing them with your package manager (sudo may ask for your password)..."
        if ! install_packages "$family" $packages; then
            echo "Could not install: $packages" >&2
            echo "Install them yourself, then run ./install.sh again (or use --no-deps to skip this step)." >&2
            exit 1
        fi
        still="$(missing_requirements)"
        if [ -n "$still" ]; then
            echo "Installed, but Python still can't load: $still" >&2
            echo "Brev may not start. See \"Install\" in README.md." >&2
        fi
    fi
fi

# --- Desktop entry and icons ---------------------------------------------------

mkdir -p "$data/applications" "$data/icons/hicolor/scalable/apps" "$data/icons/hicolor/symbolic/apps" "$HOME/.local/bin"
ln -sf "$here/bin/brev" "$HOME/.local/bin/brev"
sed "s|^Exec=brev|Exec=$here/bin/brev|" "$here/data/se.croby.Brev.desktop" > "$data/applications/se.croby.Brev.desktop"
cp "$here/data/icons/hicolor/scalable/apps/se.croby.Brev.svg" "$data/icons/hicolor/scalable/apps/"
cp "$here/data/icons/hicolor/symbolic/apps/se.croby.Brev-symbolic.svg" "$data/icons/hicolor/symbolic/apps/"
update-desktop-database "$data/applications" 2>/dev/null || true
gtk4-update-icon-cache -q -t "$data/icons/hicolor" 2>/dev/null || true
echo "Installed. Brev is now in your app grid."
