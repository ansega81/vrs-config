#!/usr/bin/env bash
# vrs-config installer: dependency checks, udev rules, app install
# (files -> /opt/vrs-config, commands -> /usr/local/bin) + usage instructions.
#
#   ./install.sh             full install (uses sudo for system changes)
#   ./install.sh --check     verify dependencies / detect the base only (no sudo)
#   ./install.sh --no-app    install udev rules only, keep running from this folder
#   ./install.sh --uninstall remove /opt/vrs-config, the commands and the udev rules
#
set -e
cd "$(dirname "$0")"

INSTALL_DIR=/opt/vrs-config
BIN_DIR=/usr/local/bin
RULE=/etc/udev/rules.d/99-vrs-dfp.rules

CHECK_ONLY=0; NO_APP=0; UNINSTALL=0
for a in "$@"; do
    case "$a" in
        --check) CHECK_ONLY=1 ;;
        --no-app) NO_APP=1 ;;
        --uninstall) UNINSTALL=1 ;;
        *) echo "unknown option: $a"; exit 1 ;;
    esac
done

have() { command -v "$1" >/dev/null 2>&1; }

if [ "$UNINSTALL" = 1 ]; then
    echo "Removing vrs-config system install (needs sudo):"
    sudo rm -rf "$INSTALL_DIR" "$BIN_DIR/vrs-config" "$BIN_DIR/vrs-config-gui" "$RULE"
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=hidraw --action=change
    echo "  removed $INSTALL_DIR, $BIN_DIR/vrs-config{,-gui}, $RULE"
    echo "  (this folder's files were not touched)"
    exit 0
fi

echo "== vrs-config install =="
echo

# ---------- 1. python ----------
echo "1) Python 3 (>= 3.9):"
if ! have python3; then
    echo "  python3 not found. Install it first, e.g.:"
    echo "    Debian/Ubuntu: sudo apt install python3"
    exit 1
fi
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "  [ok] $(python3 -V)  $(command -v python3)"
else
    echo "  [!!] python $(python3 -V) is too old (need >= 3.9)."
    exit 1
fi

# ---------- 2. tkinter (GUI only) ----------
echo "2) Tkinter (only needed for the GUI):"
if python3 -c 'import tkinter' 2>/dev/null; then
    echo "  [ok] tkinter present - GUI will work"
else
    echo "  [!!] tkinter missing - the CLI still works; for the GUI install it:"
    if   have apt-get; then echo "        sudo apt install python3-tk"
    elif have dnf;     then echo "        sudo dnf install python3-tk"
    elif have pacman;  then echo "        sudo pacman -S tk"
    elif have zypper;  then echo "        sudo zypper install python3-tk"
    else echo "        (your distro's python3-tk equivalent)"; fi
fi

# ---------- 3. udev ----------
echo "3) udev rules (user access to the wheel base's USB nodes):"
if [ "$CHECK_ONLY" = 1 ]; then
    python3 - <<'EOF'
import glob, os
found = False
for d in sorted(glob.glob('/sys/bus/usb/devices/*/')):
    try:
        v = open(os.path.join(d, 'idVendor')).read().strip()
        p = open(os.path.join(d, 'idProduct')).read().strip()
    except OSError:
        continue
    if v == '0483' and p in ('a355', 'cb52'):
        found = True
        bus = dev = None
        for l in open(os.path.join(d, 'uevent')):
            if l.startswith('BUSNUM='): bus = l[7:].strip()
            if l.startswith('DEVNUM='): dev = l[7:].strip()
        node = f'/dev/bus/usb/{int(bus):03d}/{int(dev):03d}' if bus and dev else None
        if node and os.path.exists(node) and os.access(node, os.W_OK):
            print(f'  [ok] --check: base found, {node} is user-writable (udev rules active)')
        else:
            print(f'  [!!] --check: base found at {node}, not user-writable yet - run ./install.sh without --check')
if not found:
    print('  [!!] --check: no VRS wheel base (0483:a355/cb52) on the bus right now (plug it in and re-run)')
EOF
else
    sudo install -m 0644 udev/99-vrs-dfp.rules "$RULE"
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=usb --action=change
    sudo udevadm trigger --subsystem-match=hidraw --action=change
    echo "  [ok] installed $RULE + triggered"
fi

# ---------- 4. device ----------
echo "4) Wheel base:"
found=$(python3 - <<'EOF'
import glob, os
names = {'a355': 'VRS DFP15', 'cb52': 'DirectForce Pro base'}
for d in sorted(glob.glob('/sys/bus/usb/devices/*/')):
    try:
        v = open(os.path.join(d, 'idVendor')).read().strip()
        p = open(os.path.join(d, 'idProduct')).read().strip()
    except OSError:
        continue
    if v == '0483' and p in names:
        print(f"{names[p]} (0483:{p})")
EOF
)
if [ -n "$found" ]; then echo "$found" | while read -r l; do echo "  [ok] $l"; done
else echo "  [!!] no VRS wheel base detected - plug it in (PSU on) and re-run this script"; fi

# ---------- 5. app install ----------
if [ "$CHECK_ONLY" = 0 ] && [ "$NO_APP" = 0 ]; then
    echo "5) Install app to $INSTALL_DIR + commands in $BIN_DIR:"
    sudo mkdir -p "$INSTALL_DIR" "$BIN_DIR"
    sudo cp -a vrs-config.py vrs-config-gui.py SimWheelBase.proto PROTOCOL.md \
        LICENSE README.md "$INSTALL_DIR/"
    sudo install -m 0755 /dev/null "$BIN_DIR/vrs-config"
    printf '#!/bin/sh\n# vrs-config (installed from %s)\nexec python3 "%s/vrs-config.py" "$@"\n' \
        "$INSTALL_DIR" "$INSTALL_DIR" | sudo tee "$BIN_DIR/vrs-config" > /dev/null
    sudo install -m 0755 /dev/null "$BIN_DIR/vrs-config-gui"
    printf '#!/bin/sh\n# vrs-config GUI (installed from %s)\nexec python3 "%s/vrs-config-gui.py" "$@"\n' \
        "$INSTALL_DIR" "$INSTALL_DIR" | sudo tee "$BIN_DIR/vrs-config-gui" > /dev/null
    sudo chmod +x "$BIN_DIR/vrs-config" "$BIN_DIR/vrs-config-gui"
    echo "  [ok] commands registered: $BIN_DIR/vrs-config $BIN_DIR/vrs-config-gui"
    "$BIN_DIR/vrs-config" --help 2>/dev/null | head -1 || true
else
    echo "5) App install: skipped (--check/--no-app); run from this folder"
fi

# ---------- 6. usage ----------
CMD_PFX=""
if [ "$CHECK_ONLY" = 0 ] && [ "$NO_APP" = 0 ]; then CMD_PFX="yes"; fi
cat <<EOF

== Done. How to use ================================================
EOF
if [ -n "$CMD_PFX" ]; then
cat <<'EOF'
  Installed into /opt/vrs-config, commands on your PATH.

  CLI (protocol reference: PROTOCOL.md):

    sudo vrs-config --raw versions       # hw/fw/bootloader versions
    sudo vrs-config --raw header         # profile count / active slot
    sudo vrs-config --raw profiles       # decode every profile
    sudo vrs-config --raw schema --out /tmp/live.proto
    sudo vrs-config --raw set-profile --profile 0 --set ffbForce=50 --dry-run
    sudo vrs-config --raw set-profile --profile 0 --set ffbForce=50 --allow-write

  GUI (VRS One-style; needs python3-tk):

    xhost +local:                        # allow root's window on your session (X11)
    sudo vrs-config-gui
    xhost -local:

  Remove again with:  ./install.sh --uninstall
EOF
else
cat <<'EOF'
  (not installed to PATH yet - run ./install.sh without --check/--no-app,
   or from this folder:  python3 ./vrs-config.py ... / sudo ./vrs-config-gui.py)

  CLI (protocol reference: PROTOCOL.md):

    sudo ./vrs-config.py --raw versions  # hw/fw/bootloader versions
    sudo ./vrs-config.py --raw profiles  # decode every profile
    sudo ./vrs-config.py --raw set-profile --profile 0 --set ffbForce=50 --dry-run
    sudo ./vrs-config-gui.py             # GUI (needs python3-tk)
EOF
fi
cat <<'EOF'

  Notes:
    * The tools unbind the kernel HID driver from the base while they run
      (that is what sudo is for) - replug the wheel's USB cable afterwards
      and force feedback + joystick come back.
    * If reads stall after a crashed session: vrs-config --recovery
      (plain USB reset, no power cycle needed), or the GUI's Recover button.
    * Writes always rewrite the WHOLE profile store - that is how the base
      works (the official software does the same). The tool re-reads the
      store before writing it back; keep an exported profile anyway.
    * Firmware updates are NOT supported on purpose (0x08/0x09 refused).
      Use the official Windows app for firmware, sparingly.
    * Unofficial tool; not affiliated with VRS/Smarty Co. Use at your own
      risk - though months of development on real hardware never lost data.
EOF
