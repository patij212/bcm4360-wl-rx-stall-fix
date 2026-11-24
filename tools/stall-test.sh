#!/bin/bash
# Reproduce / rule out the wl RX stall: download flat out for MINUTES (default 10)
# and count the stalls. A stall = nothing received for 3 s while downloading and
# the gateway does not answer a ping. Each stall is ended with a NetworkManager
# reconnect (the only thing that clears it) and the test carries on.
# No root needed. Run it before and after the fix, on a fast 5 GHz link.
#   bash stall-test.sh                  MINUTES=30 bash stall-test.sh
#   URL=https://your.mirror/1GB.bin bash stall-test.sh
set -u
MINUTES=${MINUTES:-10}
URL=${URL:-https://fsn1-speed.hetzner.com/1GB.bin}
ifc=$(for d in /sys/class/net/*; do
        [ "$(basename "$(readlink "$d/device/driver" 2>/dev/null)")" = wl ] && basename "$d"; done | head -1)
[ -n "$ifc" ] || { echo "no interface on the wl driver found"; exit 1; }
uuid=$(nmcli -g GENERAL.CON-UUID device show "$ifc" 2>/dev/null)
[ -n "$uuid" ] || { echo "$ifc is not connected"; exit 1; }
rx() { cat "/sys/class/net/$ifc/statistics/rx_bytes"; }
gw() { ip -4 route show default dev "$ifc" | awk '{print $3; exit}'; }

echo "stall test: $ifc, $MINUTES min, $(iw dev "$ifc" link 2>/dev/null | awk '/freq/ {print $2 " MHz"}')"
echo "driver option rx_amsdu_in_ampdu: $(cat /sys/module/wl/parameters/rx_amsdu_in_ampdu 2>/dev/null || echo 'not present (unpatched driver)')"
end=$((SECONDS + MINUTES * 60)); stalls=0; t0=$SECONDS
( while [ $SECONDS -lt $end ]; do
    curl -s -o /dev/null --speed-limit 100000 --speed-time 8 --max-time $((end - SECONDS)) "$URL"; sleep 1
  done ) &
dl=$!
trap 'kill $dl 2>/dev/null; exit 130' INT TERM
# wl's rx_bytes is a 32-bit counter: it wraps every 4 GiB (~100 s at 350 Mbit/s),
# so throughput is summed from per-second deltas instead of start/end values.
last=$(rx); still=0; tot=0; mark=$SECONDS; tm=0
while [ $SECONDS -lt $end ]; do
  sleep 1
  now=$(rx)
  d=$((now - last)); [ $d -lt 0 ] && d=$((d + 4294967296)); tot=$((tot + d))
  if [ $((SECONDS - mark)) -ge 60 ]; then
    echo "$(date +%T)   last minute: $(( (tot - tm) * 8 / (SECONDS - mark) / 1000000 )) Mbit/s"
    mark=$SECONDS; tm=$tot
  fi
  if [ "$now" = "$last" ]; then still=$((still + 1)); else still=0; fi
  last=$now
  if [ $still -ge 3 ]; then
    g=$(gw)
    if [ -n "$g" ] && ! ping -n -q -c2 -W1 -I "$ifc" "$g" >/dev/null 2>&1; then
      stalls=$((stalls + 1)); s=$SECONDS
      echo "$(date +%T) stall $stalls: nothing received for 3 s, gateway silent; reconnecting"
      nmcli --wait 30 connection up uuid "$uuid" >/dev/null 2>&1
      echo "$(date +%T)   back after $((SECONDS - s)) s"
    fi
    still=0; now=$(rx); d=$((now - last)); [ $d -lt 0 ] && d=$((d + 4294967296)); tot=$((tot + d)); last=$now
  fi
done
kill $dl 2>/dev/null; wait $dl 2>/dev/null
dur=$((SECONDS - t0))
echo "result: $stalls stall(s) in $dur s, average $(( tot * 8 / dur / 1000000 )) Mbit/s"
