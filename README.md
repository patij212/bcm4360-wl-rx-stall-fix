# BCM4360 `wl` driver: fix for RX stalls under load (CCMP replay drops)

If your Broadcom BCM4360 (or other `wl`/broadcom-sta card) **stops receiving during big
downloads or streaming** (still "connected", sending works, nothing comes back until
you reconnect Wi-Fi), this may be your bug. On the machine it was found on, it
happened about **once a minute at 200–300 Mbit/s**. With this fix it went **20 minutes
and 44.7 GB at an average 298 Mbit/s without a single stall**.

The fix is one driver setting, `rx_amsdu_in_ampdu=0`, plus a small patch to the
broadcom-sta source and an 11-byte unlock of your local copy of the binary blob,
because Broadcom's blob refuses that setting otherwise.

> Tested on one card with one router. Please run the stall test before and after,
> and report back either way.

**New to this?** [docs/how-we-found-it.md](docs/how-we-found-it.md) explains the bug and
how it was tracked down in plain language.

## What happens

During a stall, the firmware counters (read with `tools/wlctl.py`, which needs patch
0002) show every unicast data frame from the AP being thrown away as a **replay**:

```
during one stall:  ucast-from-AP 36   undecryptable 36   replay 36
                  (rxdfrmucastmbss)   (rxundec)          (ccmpreplay)
```

The driver's stored CCMP packet number for the link has jumped ahead of the AP's, so
every new frame looks old. A reconnect installs new keys and resets the counter, which
is why toggling Wi-Fi "fixes" it.

This was measured directly with a debug build that exposes the blob's per-key receive
counters (`tools/keyview-debug.patch`: read-only `/proc/wl_keys`, no key bytes; offsets
are specific to 6.30.223.271 amd64). With `rx_amsdu_in_ampdu=1`, in three stalls the stored
PN jumped from about 22k to 80k up to **8.4 M, 2.1 M and 1.08 G** in the same 200 ms interval
in which the replay drops began. It then stayed frozen until the reconnect. The low 16 bits
kept advancing normally, and only the high 32 bits took a wrong value (0x80, 0x20, 0x40a1).
With the option at 0, 60 s at full speed showed steady growth and 0 replays. Interrupts, DMA, ASPM, IOMMU and signal were all
ruled out. It also happens within seconds of a fresh driver load.

### How the bad number gets in (traced)

A separate GPL debug module (`tools/wltrace/`, kprobes on the blob) recorded every rx
buffer passed to `wlc_recv` and the frame behind every stored-PN jump. In the 4 jumps
where every buffer was recorded, the sequence was identical:

1. With hardware A-MSDU deaggregation, each subframe arrives in its own DMA buffer with
   its own d11 rx header. RxStatus2 bits 1–2 say first/middle/last. Bits 10–15 hold a
   running 6-bit piece counter.
2. Partway through an MPDU, a buffer tagged **first** arrives at the position of the
   next middle or last piece. It has an odd length (1462–1882 vs 1566), and rx-header
   word 1 is 0x0200 (zero on every other piece). Its 802.11 header has the right
   sequence number and usually the right PN[0..1], but 3–15 flipped bits in
   QoS/key-ID/PN[2..5].
3. The piece counter then skips 3–31 pieces. Once, a buffer starting with frame data
   instead of an rx header was also seen.
4. `wlc_recvamsdu` looks only at the first/middle/last bits. The odd "first" flushes
   the open chain and starts a new one. The middle/last pieces of a **later** MPDU
   complete it.
5. The chain goes up with the later MPDU's last-piece status: decrypt attempted, no
   decrypt or FCS error. `wlc_recvdata_ordered` parses the PN from the head's damaged
   header. It passes the replay check (it is larger) and is stored by
   `wlc_wsec_rxiv_update` before deaggregation. Every later frame is then a replay.

The 6 jumps in an earlier traced run, which logged only the jump frame, show the same
signs: odd head length, 2–3 pieces instead of 4, clean last-piece status, and flipped
bits in the head's header.

Card counters over 60 s of downloads each:

| counter | `rx_amsdu_in_ampdu=1` | `=0` |
|---|---|---|
| `rxfrmtoolong` | 115 | 0 |
| `rxgiant` | 139 | 0 |
| `rxf0ovfl` | 145 | 46 |
| `ccmpreplay` | 805 | 0 |

In the traced run there were 1.69 M first pieces vs 1.58 M last pieces. Only one piece
had the FCS-error bit, so FCS checking does not catch this. Unknown: why the MAC
produces the odd piece, whether the bit errors happen on air or in the card, and what
word 1 = 0x0200 means. A robust fix inside the blob would discard a chain whose piece
counter is not continuous. That is not possible from the open wrapper, so the fix
avoids nested A-MSDUs altogether.

## Root cause (by experiment)

A/B tests switched one setting at a time under the same 1 GB download load. Stalls
were counted by a watchdog that detects and recovers them.

| setting | stalls | throughput |
|---|---|---|
| `ampdu_rx=1` (default), 240 s | 4 | 190 Mbit/s |
| `ampdu_rx=0`, 300 s | **0** | 60 Mbit/s |
| `rx_amsdu_in_ampdu=1` (default), 240 s | 3 | 232 Mbit/s |
| `rx_amsdu_in_ampdu=0`, 300 s | **0** | **346 Mbit/s** |
| fixed driver, 20 min soak | **0** | 298 Mbit/s (44.7 GB) |

### Clean check against the unmodified driver

To rule out that the stall came from anything else changed while debugging, it was
re-tested from scratch with `tools/stall-test.sh` (watchdog off):

| driver | stalls | throughput |
|---|---|---|
| Ubuntu's `broadcom-sta-dkms`, unmodified, 10 min | **5** | (not measured reliably; see below) |
| stock + patch 0001 + unlock only, installed with `apply.sh`, 10 min | **0** | 347 Mbit/s (every minute 325–358) |
| same build, `modprobe wl rx_amsdu_in_ampdu=1`, 5 min | **8** | 194 Mbit/s |

The first stall on the stock driver came within a minute. The same build flips
between 0 and 8 stalls with nothing but the module option, so the option is what
stops it. (The stock run's throughput isn't given because the first version of the
test tool read `rx_bytes`, which this driver keeps as a 32-bit counter that wraps
every 4 GiB. The tool now sums per-second deltas.)

So the closed binary mishandles the packet number of **A-MSDUs carried inside
A-MPDUs** (802.11ac "A-MSDU in A-MPDU"). This is plausibly the same pitfall mac80211
handles with `RX_FLAG_ALLOW_SAME_PN`: A-MSDU subframes share one PN. Turning off all
A-MPDU receive also stops it but costs about 80% of the speed. Declining only the
nested A-MSDUs costs nothing.

How the setting is used (from disassembly of `wlc_hybrid.o_amd64`, broadcom-sta
6.30.223.271): `rx_amsdu_in_ampdu` is a single byte in the `wlc` state. It is set to 1
in `wlc_attach`, changed only by its iovar, and read only in
`wlc_ampdu_recv_addba_req_resp`, i.e. when the driver answers the AP's ADDBA request.
With it at 0, the A-MSDU-supported bit in our ADDBA response is cleared. That is
standard 802.11, and the AP simply sends plain A-MPDUs. Setting it once right after
`wlc_attach` therefore covers every block-ack session.

## Why the blob has to be touched: the iovar whitelist

On the hybrid Linux build, almost all driver settings (iovars) outside the core table
return `BCME_UNSUPPORTED` (-23), including `ampdu*`, `amsdu*`, `rxchain`, `txchain`
and even `bcmerror`. The reason is in `wlc_iovar_check` (offset `0x36005` in
`wlc_hybrid.o_amd64`): if the iovar table entry's flags lack bit **0x4000**, it
returns -23 before anything else is checked. Every iovar that works has that bit;
every one that fails lacks it.

`tools/unlock-iovars.py` sets bit 0x4000 on 11 named entries (the A-MPDU/A-MSDU
settings and `bcmerror`). It locates each entry through its ELF relocation to the
name string, refuses anything ambiguous, and changes exactly one 16-bit field per
entry. It only edits **your own local copy**. The modified blob is not distributed
here, because Broadcom's license does not allow it.

## Files

| file | what |
|---|---|
| `patches/0001-wl-decline-A-MSDU-inside-A-MPDU.patch` | **the fix**: module option `rx_amsdu_in_ampdu` (default 0), applied after attach |
| `patches/0002-wl-restore-private-ioctl-on-5.15plus.patch` | optional: makes `wlctl.py` work on kernels ≥ 5.15 (root full access, others read `counters`) |
| `patches/0003-wl_cfg80211-fix-spurious-disconnects-during-join.patch` | optional, independent: four join/disconnect bugs in the open cfg80211 glue (spurious "wrong password" after drops) |
| `tools/unlock-iovars.py` | sets the whitelist bit in your local `wlc_hybrid.o_*` (`--check` = dry run) |
| `tools/stall-test.sh` | reproduces the stall and counts it, no root; recovers each stall by reconnecting |
| `tools/wlctl.py` | reads firmware counters and iovars (`counters`, `watch`, `get`, `set`) |
| `tools/wltrace/` | debug only: separate GPL kprobe module that records each stored-PN jump with the frames and rx buffers around it (`/proc/wl_rxtrace`), plus decoders; offsets for 6.30.223.271 amd64 only, refuses to arm otherwise. Its raw output contains MAC addresses; the decoders do not print them |
| `tools/keyview-debug.patch` | debug only: read-only `/proc/wl_keys` with the blob's stored receive PNs (no key bytes); offsets for 6.30.223.271 amd64 only; applies on top of 0001 and 0002 |
| `apply.sh` | Debian/Ubuntu: builds a separate DKMS version `+amsdufix`; `--undo` reverts |

Patches are against `broadcom-sta` 6.30.223.271 as shipped by Debian/Ubuntu
(`broadcom-sta-dkms` 6.30.223.271-29ubuntu1, i.e. with the Debian patch series
applied). They should apply to most packagings with little or no fuzz. The fix itself
(0001) only needs the line that sets `qtxpower` after `wlc_attach` as context.

## Try it

1. Measure first, on a fast 5 GHz link: `bash tools/stall-test.sh` (10 min of downloads).
2. Debian/Ubuntu: `sudo bash apply.sh`. Then check:
   ```
   cat /sys/module/wl/parameters/rx_amsdu_in_ampdu    # 0
   journalctl -k | grep rx_amsdu_in_ampdu=            # "... set (bcmerror 0)"
   ```
   If the log says `FAILED`, the unlock did not happen and nothing changed.
3. Measure again: `bash tools/stall-test.sh`.
4. To compare without rebuilding: `sudo modprobe -r wl && sudo modprobe wl rx_amsdu_in_ampdu=1`
   brings back Broadcom's behaviour until the next reload.

Other distros: copy your wl source somewhere, apply 0001 (and 0002 if you want
`wlctl.py`), run `python3 tools/unlock-iovars.py <source>/lib/<blob>.o*`, then build as
you normally would. Arch's `broadcom-wl-dkms` keeps the source under
`/usr/src/broadcom-wl-<version>`; `apply.sh` accepts `SRC=... NAME=broadcom-wl`, but it
has only been run on Ubuntu.

## Limits and unknowns

- Shown on **one** BCM4360 (14e4:43a0, desktop PCIe card), one ISP-supplied 802.11ac
  router (5 GHz, WPA2/CCMP), Ubuntu 26.04 with kernel 7.0. APs that never send A-MSDU
  inside A-MPDU will not trigger the bug, so the fix does nothing there.
- amd64 blob only. The i386 blob, if anyone still has one, would need its own offsets
  checked; the script searches by name and should still work.
- 2.4 GHz not tested.
- The unlock also allows *setting* the other A-MPDU iovars as root. That is fine for
  testing, but they are not needed for the fix.

## Licence

The patches modify the open (ISC-style licensed) part of broadcom-sta and are offered
under the same terms. The tools are free to use and modify. The Broadcom binary blob is
not included or redistributed. `tools/wltrace/wltrace.c` is a separate kernel module under GPL-2.0, because
kprobes are GPL-only; it is not linked into `wl`.
