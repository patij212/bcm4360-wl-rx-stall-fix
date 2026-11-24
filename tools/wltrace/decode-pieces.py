#!/usr/bin/env python3
"""Decode wltrace v2 piece windows: per received buffer the hardware status, A-MSDU piece
type, and (for first pieces) sequence number and packet number. MACs are not printed."""
import re, sys
AGG = ["mid", "FIRST", "LAST", "one"]
def hdrinfo(h):
    if len(h) < 34: return ""
    fc = h[0] | h[1] << 8
    seq = (h[22] | h[23] << 8) >> 4; qos = h[24] | h[25] << 8; iv = h[26:34]
    pn = iv[0] | iv[1] << 8 | iv[4] << 16 | iv[5] << 24 | iv[6] << 32 | iv[7] << 40
    return f"fc={fc:04x} seq={seq:4d} qos={qos:04x} kid={iv[3]:02x} PN={pn:#014x}"
def piece(raw, rxhlen):
    w = [raw[k] | raw[k + 1] << 8 for k in range(0, 32, 2)]
    s1, s2 = w[8], w[9]
    off = rxhlen + (2 if s1 & 4 else 0)
    amsdu = s2 & 1; agg = AGG[(s2 >> 1) & 3] if amsdu else "-"
    flags = ("FCSERR " if s1 & 1 else "") + ("DECERR " if s1 & 0x10 else "") + ("phyerr " if w[2] & 0x310 else "")
    info = ""
    if not amsdu or agg in ("FIRST", "one"):
        info = hdrinfo(raw[off + 6:])
    return f"s1={s1:04x} s2={s2:04x} {agg:5} fsz={w[0]:5d} tsf={w[10]:04x} avb={w[13]:04x}{w[12]:04x} {flags}{info}"
txt = open(sys.argv[1]).read()
print(txt.splitlines()[0]); print(txt.splitlines()[1])
for blk in txt.split("=== trigger")[1:]:
    L = blk.splitlines(); print("\n=== trigger" + L[0])
    for l in L:
        if l.startswith("  JUMP"):
            m = re.search(r"seq=(\S+).*len=(\d+) chain=(\d+)", l); print("  JUMP seq", m[1], "len", m[2], "chain", m[3])
        elif l.startswith("  chain:") or l.startswith("  pieces"):
            print(l)
        elif l.startswith("  pre "):
            print("  pre (64 bytes before head header):", l.split()[1][:80], "...")
        else:
            m = re.match(r"\s+(m\d)?\s*age=(-?\d+) t=(\S+) cpu(\d+) len=(\d+) st=(\d+) err=(\d+) rxhlen=(\d+) raw (\S+)", l)
            if m:
                raw = bytes.fromhex(m[9])
                print(f"   {m[1] or '  '} age={m[2]:>5} t={m[3][-9:]} len={m[5]:>4} st={m[6]} err={m[7]:>3} {piece(raw, int(m[8]))}")
