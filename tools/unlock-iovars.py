#!/usr/bin/env python3
"""Let the wl driver accept settings that Broadcom left off its whitelist.

The closed part of the driver (lib/wlc_hybrid.o_amd64) checks every driver setting
("iovar") in wlc_iovar_check (offset 0x36005): if the entry's flags lack 0x4000 it
returns BCME_UNSUPPORTED (-23) without looking further. The A-MPDU settings
(ampdu_rx, ampdu, ...) and bcmerror lack that bit, which is why they always failed.

Each table entry is 16 bytes: name pointer, u16 id, u16 flags, u32 type. The name
pointer is filled in by a relocation, so this script finds an entry by its
relocation to the name string, checks it, and sets 0x4000 in its flags. Nothing
else in the file changes. Safe to run again (already-set entries are left alone).

  unlock-iovars.py OBJECT [--check] [NAME ...]     default: the A-MPDU set + bcmerror
"""
import re
import struct
import subprocess
import sys

DEFAULT = ["ampdu_rx", "ampdu", "ampdu_tx", "ampdu_ba_wsize", "ampdu_density",
           "ampdu_rx_tid", "ampdu_rx_density", "ampdu_rx_factor",
           "amsdu", "rx_amsdu_in_ampdu", "bcmerror"]
WHITELIST = 0x4000


def sections(obj):
    out = subprocess.run(["readelf", "-SW", obj], capture_output=True, text=True, check=True).stdout
    sec = {}
    for line in out.splitlines():
        m = re.match(r"\s*\[\s*\d+\]\s+(\S+)\s+\S+\s+[0-9a-f]+\s+([0-9a-f]+)\s+([0-9a-f]+)", line)
        if m:
            sec[m.group(1)] = (int(m.group(2), 16), int(m.group(3), 16))
    return sec


def entries(obj, data, sec):
    """name -> list of file offsets of iovar entries whose name pointer points at it."""
    ro_off, ro_size = sec[".rodata"]
    st_off, _ = sec[".rodata.str1.1"]
    out = subprocess.run(["objdump", "-r", "-j", ".rodata", obj],
                         capture_output=True, text=True, check=True).stdout
    found = {}
    for line in out.splitlines():
        m = re.match(r"([0-9a-f]{16}) R_X86_64_64\s+\.rodata\.str1\.1\+0x([0-9a-f]+)", line)
        if not m:
            continue
        addr, soff = int(m.group(1), 16), int(m.group(2), 16)
        if addr + 16 > ro_size:
            continue
        end = data.index(b"\0", st_off + soff)
        name = data[st_off + soff:end].decode(errors="replace")
        found.setdefault(name, []).append(ro_off + addr)
    return found


def main(argv):
    if not argv:
        sys.exit(__doc__)
    obj, rest = argv[0], argv[1:]
    check = "--check" in rest
    names = [a for a in rest if a != "--check"] or DEFAULT
    data = bytearray(open(obj, "rb").read())
    sec = sections(obj)
    found = entries(obj, data, sec)
    changed = 0
    for name in names:
        # a real iovar entry: small id and a small type code (low 16 bits of the
        # type word); plain string tables elsewhere in .rodata fail this
        offs = [o for o in found.get(name, [])
                if struct.unpack_from("<H", data, o + 8)[0] < 1024
                and struct.unpack_from("<H", data, o + 12)[0] < 0x20]
        if len(offs) != 1:
            sys.exit(f"{name}: expected one iovar entry, found {len(offs)}; nothing written")
        o = offs[0]
        vid, flags = struct.unpack_from("<HH", data, o + 8)
        state = "already allowed" if flags & WHITELIST else ("would allow" if check else "allowed")
        print(f"{name:20} id {vid:3}  flags {flags:#06x}  {state}")
        if not flags & WHITELIST:
            struct.pack_into("<H", data, o + 10, flags | WHITELIST)
            changed += 1
    if changed and not check:
        open(obj, "wb").write(data)
    print(f"{changed} entr{'y' if changed == 1 else 'ies'} {'to change' if check else 'changed'}")


if __name__ == "__main__":
    main(sys.argv[1:])
