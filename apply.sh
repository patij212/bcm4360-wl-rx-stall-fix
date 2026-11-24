#!/bin/bash
# Build and install broadcom-sta (wl) with the RX-stall fix, as a separate DKMS
# version next to the stock one. Debian/Ubuntu broadcom-sta-dkms layout.
#   sudo bash apply.sh               fix + ioctl patch (0001, 0002)
#   sudo PATCHES="0001 0002 0003" bash apply.sh     also the join fixes
#   sudo bash apply.sh --undo        back to the stock driver
# Other distros: SRC=/usr/src/<your wl source> NAME=<dkms package name> (see README).
# Wi-Fi drops for a few seconds while the driver reloads.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
VER=${VER:-6.30.223.271}
NAME=${NAME:-broadcom-sta}
SRC=${SRC:-/usr/src/$NAME-$VER}
FIX=$VER+amsdufix
DST=/usr/src/$NAME-$FIX
KVER=$(uname -r)
PATCHES=${PATCHES:-"0001 0002"}

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
if [ "${1:-}" = "--undo" ]; then
  dkms remove -m "$NAME" -v "$FIX" --all || true
  rm -rf "$DST"
  dkms install -m "$NAME" -v "$VER" -k "$KVER" --force
  modprobe -r wl && modprobe wl
  echo "Stock driver back."; exit 0
fi
[ -d "$SRC" ] || { echo "Stock source not found at $SRC (set SRC=...)"; exit 1; }
for t in patch python3 readelf objdump dkms; do command -v $t >/dev/null || { echo "needs $t"; exit 1; }; done
# dkms fails half-way when /tmp is full
[ "$(df --output=avail -k /tmp | tail -1)" -ge 524288 ] || export TMPDIR=/var/tmp

echo "== Copying $SRC to $DST"
dkms remove -m "$NAME" -v "$FIX" --all >/dev/null 2>&1 || true
rm -rf "$DST"; cp -a "$SRC" "$DST"
for p in $PATCHES; do
  f=$(ls "$HERE"/patches/"$p"-*.patch)
  echo "== Applying $(basename "$f")"
  patch -d "$DST" -p1 --forward --no-backup-if-mismatch < "$f"
done
blob=$(ls "$DST"/lib/*.o* | head -1)
echo "== Unlocking the driver settings in $(basename "$blob") (your local copy only)"
python3 -I "$HERE/tools/unlock-iovars.py" "$blob"
sed -i "s/^PACKAGE_VERSION=.*/PACKAGE_VERSION=\"$FIX\"/" "$DST/dkms.conf"

echo "== Building (about 30 s)"
dkms add -m "$NAME" -v "$FIX"
dkms build -m "$NAME" -v "$FIX" -k "$KVER" || { echo "Build failed; stock driver untouched."; dkms remove -m "$NAME" -v "$FIX" --all; rm -rf "$DST"; exit 1; }
echo "== Installing for $KVER (the stock version stays registered for other kernels)"
dkms remove -m "$NAME" -v "$VER" -k "$KVER" >/dev/null 2>&1 || true
dkms install -m "$NAME" -v "$FIX" -k "$KVER" --force
echo "== Reloading wl"
modprobe -r wl && modprobe wl
sleep 2
echo
echo "module option rx_amsdu_in_ampdu: $(cat /sys/module/wl/parameters/rx_amsdu_in_ampdu 2>/dev/null || echo MISSING)"
journalctl -k -n 100 -o cat | grep "rx_amsdu_in_ampdu=" | tail -1
echo "Undo any time: sudo bash $0 --undo"
