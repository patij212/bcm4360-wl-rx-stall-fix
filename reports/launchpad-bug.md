Title: broadcom-sta-dkms: BCM4360 stops receiving under load (CCMP replay drops); fix: decline A-MSDU in A-MPDU

Package: broadcom-sta-dkms 6.30.223.271-29ubuntu1, Ubuntu 26.04, kernel 7.0.0-44-generic, BCM4360 [14e4:43a0]

With the wl driver on 5 GHz 802.11ac, reception stops during sustained downloads (here about once a minute at 200-300 Mbit/s): still associated, transmit works, nothing received until a reconnect. To the user it looks like Wi-Fi freezing until it is switched off and on.

During a stall the firmware counters show every unicast frame from the AP dropped as a CCMP replay (rxdfrmucastmbss == rxundec == ccmpreplay). A/B tests:

  rx_amsdu_in_ampdu=1 (default): 3 stalls in 4 min, 232 Mbit/s
  rx_amsdu_in_ampdu=0:           0 stalls in 5 min, 346 Mbit/s
  fixed driver, 20 min soak:     0 stalls, 298 Mbit/s average

Re-checked from scratch: the unmodified Ubuntu driver stalled 5 times in
10 min (first within a minute); stock + only the fix: 0 stalls in 10 min at
347 Mbit/s; the same build with the option set back to 1: 8 stalls in 5 min.

The stored replay counter was also read directly, through a read-only debug /proc file (no key bytes) that exposes the blob's per-key receive PNs. In three stalls it jumped from about 22k-80k to 8.4M, 2.1M and 1.08G in the same 200 ms interval in which the replay drops began, and stayed there until the reconnect. The low 16 bits kept advancing; only the high 32 bits took a wrong value. With the option at 0 it grew steadily, with no replays.

Traced further with a separate kprobe module (in the repo), the bad PN comes from a mis-assembled A-MSDU. With hardware deaggregation, a buffer tagged "first" sometimes arrives mid-MPDU. It has an odd length and 3-15 flipped bits in the QoS/key-ID/PN[2..5] bytes of its 802.11 header. The RxStatus2 piece counter then skips 3-31 pieces. wlc_recvamsdu looks only at the first/middle/last bits, so it completes the chain with a later MPDU's middle/last pieces. That last piece's clean decrypt status is trusted, and the PN parsed from the damaged head is stored. Seen directly in 4 jumps, with the same signs in 6 more. Card counters per 60 s, option at 1 vs 0: rxfrmtoolong 115/0, rxgiant 139/0, ccmpreplay 805/0.

The flag is only read when the driver builds its ADDBA response, so with 0 the AP stops nesting A-MSDUs inside A-MPDUs (standard 802.11 behaviour, no speed loss here).

The hybrid blob rejects this iovar with BCME_UNSUPPORTED unless the iovar table entry has flag 0x4000 (wlc_iovar_check). A small script sets that bit on the local blob copy during the DKMS build (11 bytes, located via ELF relocations; nothing redistributed) and a short patch sets the option after wlc_attach.

Patches (against this package's source), the unlock script, a stall test and the full write-up:
https://github.com/patij212/bcm4360-wl-rx-stall-fix

Tested on one card and one router so far; reports from other BCM4360 users welcome.
