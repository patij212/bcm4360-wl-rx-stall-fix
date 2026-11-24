Title: RX stalls under load (CCMP replay drops) – fix: decline A-MSDU inside A-MPDU

With a BCM4360 on 5 GHz 802.11ac, `wl` stops receiving during sustained downloads (here about once a minute at 200–300 Mbit/s). It stays associated and transmit works, but nothing is received until a reconnect.

During a stall the firmware counters show every unicast frame from the AP dropped as a CCMP replay (`rxdfrmucastmbss == rxundec == ccmpreplay`). A/B tests point at A-MSDUs carried inside A-MPDUs:

| setting | stalls | throughput |
|---|---|---|
| `rx_amsdu_in_ampdu=1` (default), 4 min | 3 | 232 Mbit/s |
| `rx_amsdu_in_ampdu=0`, 5 min | 0 | 346 Mbit/s |
| fixed driver, 20 min soak | 0 | 298 Mbit/s |
| unmodified stock driver, 10 min | 5 | – |
| stock + this fix only, 10 min | 0 | 347 Mbit/s |
| same build, option back to 1, 5 min | 8 | 194 Mbit/s |

The stored replay counter was also read directly (a read-only debug `/proc` file exposing the blob's per-key receive PNs, no key bytes). In three stalls it jumped from about 22k–80k to 8.4M, 2.1M and 1.08G in the same 200 ms interval in which the replay drops began, and stayed there until the reconnect. The low 16 bits kept advancing; only the high 32 bits went wrong. With the option at 0 it grew steadily with no replays.

Traced further with a separate kprobe module (in the repo), the bad PN comes from a mis-assembled A-MSDU. With hardware deaggregation, a buffer tagged "first" sometimes arrives mid-MPDU. It has an odd length and 3-15 flipped bits in the QoS/key-ID/PN[2..5] bytes of its 802.11 header. The RxStatus2 piece counter then skips 3-31 pieces. `wlc_recvamsdu` looks only at the first/middle/last bits, so it completes the chain with a later MPDU's middle/last pieces. That last piece's clean decrypt status is trusted, and the PN parsed from the damaged head is stored. Seen directly in 4 jumps, with the same signs in 6 more. Card counters per 60 s, option at 1 vs 0: `rxfrmtoolong` 115/0, `rxgiant` 139/0, `ccmpreplay` 805/0.

The flag is only read when building the ADDBA response, so with 0 the AP just stops nesting A-MSDUs in A-MPDUs.

The hybrid blob rejects this iovar with `BCME_UNSUPPORTED` unless the iovar table entry has flag `0x4000` (checked in `wlc_iovar_check`). A small script sets that bit on the local blob copy at build time (11 bytes, located via ELF relocations; nothing redistributed), and a ~10-line change sets the option after `wlc_attach`.

Patches, script, stall test and write-up: https://github.com/patij212/bcm4360-wl-rx-stall-fix

Caveats: tested on one card and one router (Ubuntu 26.04, kernel 7.0). Patch 0001's context is the Debian-patched source (the line that sets `qtxpower` after `wlc_attach`), so it may need re-diffing against Arch's patch set; the change itself is small.
