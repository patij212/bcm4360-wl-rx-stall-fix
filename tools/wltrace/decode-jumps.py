#!/usr/bin/env python3
"""Decode /proc/wl_rxtrace dumps: per event the 802.11 header fields, the PN from the
IV bytes, A-MSDU flags and hardware rx status. MAC addresses are not printed."""
import re, sys
def dec(line_ev, hdr, rxh, f30):
    h = bytes.fromhex(hdr); r = bytes.fromhex(rxh); f = bytes.fromhex(f30)
    fc = h[0] | h[1] << 8
    dur = h[2] | h[3] << 8
    seq = (h[22] | h[23] << 8) >> 4
    qos = h[24] | h[25] << 8
    iv = h[26:34]
    pn = iv[0] | iv[1] << 8 | iv[4] << 16 | iv[5] << 24 | iv[6] << 32 | iv[7] << 40
    a2_eq_a3 = h[10:16] == h[16:22]
    rs1 = r[16] | r[17] << 8; rs2 = r[18] | r[19] << 8
    agg = ["mid", "FIRST", "LAST", "single"][(rs2 >> 1) & 3]
    return (f"fc={fc:04x} dur={dur:04x} seq={seq:4d} qos={qos:04x} tid={qos & 7} amsdu={qos >> 7 & 1} "
            f"keyid-byte={iv[3]:02x} IV-PN={pn:#014x} a2==a3:{int(a2_eq_a3)} "
            f"rxs1={rs1:04x} rxs2={rs2:04x}({agg}) fsize={r[0] | r[1] << 8}")
txt = open(sys.argv[1]).read()
for blk in txt.split("=== trigger")[1:]:
    print("=== trigger" + blk.splitlines()[0])
    lines = blk.splitlines()[1:]
    i = 0
    while i < len(lines):
        l = lines[i]
        m = re.match(r"\s+(before|JUMP)\s+t=(\S+) .*slot=(\d) old=(\d+) new=(\d+).*len=(\d+) chain=(\d+)", l)
        if m:
            f30 = lines[i + 1].split()[1]; hdr = lines[i + 2].split()[1]; rxh = lines[i + 3].split()[1]
            tag = "JUMP" if m[1] == "JUMP" else "    "
            print(f"{tag} t={m[2][-9:]} slot={m[3]} stored-before={int(m[4]):#x} len={m[6]:>4} chain={m[7]} {dec(l, hdr, rxh, f30)}")
            i += 4; continue
        if "replay" in l:
            print("   ", l.strip())
        i += 1
