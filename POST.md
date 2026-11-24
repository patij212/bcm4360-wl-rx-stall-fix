Title: broadcom-sta (wl) BCM4360: RX stalls under load are CCMP replay drops on A-MSDU-in-A-MPDU; fix + whitelist unlock

Symptom: under sustained download (YouTube, big files, ~100+ Mbit/s) a BCM4360 on the
`wl` driver stops receiving. It stays associated and transmit works, but nothing comes
back until a reconnect. Here it happened about once a minute at 200–300 Mbit/s.

Cause, by A/B testing: during a stall every unicast frame from the AP is dropped as a
CCMP replay (rxdfrmucastmbss == rxundec == ccmpreplay). Disabling `rx_amsdu_in_ampdu`
stops it completely, with no speed loss:

- rx_amsdu_in_ampdu=1 (default): 3 stalls in 4 min, 232 Mbit/s
- rx_amsdu_in_ampdu=0: 0 stalls in 5 min, 346 Mbit/s; then 0 stalls in a 20-min soak (298 Mbit/s, 44.7 GB)

The setting only controls the A-MSDU bit in the driver's ADDBA response, so the AP
just stops nesting A-MSDUs in A-MPDUs.

Catch: the hybrid blob refuses almost all non-core iovars (ampdu*, amsdu*, rxchain,
bcmerror...) with BCME_UNSUPPORTED, because `wlc_iovar_check` requires flag 0x4000 in
the iovar table entry. A small script sets that bit on your *local* blob copy (11 bytes,
located by ELF relocation; nothing redistributed), and a 30-line patch sets the option
at load. There is also an optional patch restoring the private ioctl on kernels ≥ 5.15
for diagnostics, plus a stall test script.

Tested on one card and one router only, so before/after reports would be very welcome:
<link to the files>
