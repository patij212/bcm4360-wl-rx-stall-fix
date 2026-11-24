#!/usr/bin/env python3
"""wlctl: read internal state of the Broadcom 'wl' driver through its private ioctl.

  wlctl.py counters          all non-zero firmware/driver counters
  wlctl.py counters --json   every counter as JSON (used by the stall capture)
  wlctl.py watch [secs]      per-interval deltas of the counters that matter for RX stalls
  wlctl.py get <iovar>       raw value of any iovar (int + hex)
  wlctl.py set <iovar> <int>   (root) change an integer iovar, e.g. set ampdu_rx 0
  wlctl.py down | up         (root) stop / restart the MAC (some iovars need it down)
  wlctl.py getstr <iovar> [arg]   text result of an iovar (e.g. 'dump' if the blob supports it)
  wlctl.py dbg               shim debug counters (/proc/wl_dbg), only on instrumented builds
  wlctl.py record [secs]     one line per interval: what changed in the netdev, IRQ, shim,
                             firmware counters and card registers; flags RX stalls

Needs patches/0002 on kernels >= 5.15: they only route SIOCDEVPRIVATE to
ndo_siocdevprivate, which the stock broadcom-sta does not implement.
"""
import ctypes, fcntl, json, os, socket, struct, sys, time

SIOCDEVPRIVATE = 0x89F0
WLC_UP, WLC_DOWN, WLC_GET_VAR, WLC_SET_VAR = 2, 3, 262, 263
BUFLEN = 8192

# wl_cnt_t version 10 (include/wlioctl.h); version + length are uint16, the rest uint32
CNT_FIELDS = """txframe txbyte txretrans txerror txctl txprshort txserr txnobuf txnoassoc txrunt txchit txcmiss txuflo txphyerr txphycrs rxframe rxbyte rxerror rxctl rxnobuf rxnondata rxbadds rxbadcm rxfragerr rxrunt rxgiant rxnoscb rxbadproto rxbadsrcmac rxbadda rxfilter rxoflo rxuflo[6] d11cnt_txrts_off d11cnt_rxcrc_off d11cnt_txnocts_off dmade dmada dmape reset tbtt txdmawar pkt_callback_reg_fail txallfrm txrtsfrm txctsfrm txackfrm txdnlfrm txbcnfrm txfunfl[8] txtplunfl txphyerror rxfrmtoolong rxfrmtooshrt rxinvmachdr rxbadfcs rxbadplcp rxcrsglitch rxstrt rxdfrmucastmbss rxmfrmucastmbss rxcfrmucast rxrtsucast rxctsucast rxackucast rxdfrmocast rxmfrmocast rxcfrmocast rxrtsocast rxctsocast rxdfrmmcast rxmfrmmcast rxcfrmmcast rxbeaconmbss rxdfrmucastobss rxbeaconobss rxrsptmout bcntxcancl rxf0ovfl rxf1ovfl rxf2ovfl txsfovfl pmqovfl rxcgprqfrm rxcgprsqovfl txcgprsfail txcgprssuc prs_timeout rxnack frmscons txnack txglitch_nack txburst txfrag txmulti txfail txretry txretrie rxdup txrts txnocts txnoack rxfrag rxmulti rxcrc txfrmsnt rxundec tkipmicfaill tkipcntrmsr tkipreplay ccmpfmterr ccmpreplay ccmpundec fourwayfail wepundec wepicverr decsuccess tkipicverr wepexcluded txchanrej psmwds phywatchdog prq_entries_handled prq_undirected_entries prq_bad_entries atim_suppress_count bcn_template_not_ready bcn_template_not_ready_done late_tbtt_dpc rx1mbps rx2mbps rx5mbps5 rx6mbps rx9mbps rx11mbps rx12mbps rx18mbps rx24mbps rx36mbps rx48mbps rx54mbps rx108mbps rx162mbps rx216mbps rx270mbps rx324mbps rx378mbps rx432mbps rx486mbps rx540mbps pktengrxducast pktengrxdmcast rfdisable bphy_rxcrsglitch bphy_badplcp txexptime txmpdu_sgi rxmpdu_sgi txmpdu_stbc rxmpdu_stbc rxundec_mcst tkipmicfaill_mcst tkipcntrmsr_mcst tkipreplay_mcst ccmpfmterr_mcst ccmpreplay_mcst ccmpundec_mcst fourwayfail_mcst wepundec_mcst wepicverr_mcst decsuccess_mcst tkipicverr_mcst wepexcluded_mcst dma_hang reinit pstatxucast pstatxnoassoc pstarxucast pstarxbcmc pstatxbcmc cso_passthrough cso_normal chained chainedsz1 unchained maxchainsz currchainsz pciereset cfgrestore reinitreason[8] """.split()

# Counters that say where received frames are lost: radio -> MAC FIFO -> DMA ring -> host
KEY = ["rxframe", "rxbyte", "rxdfrmucastmbss", "rxbeaconmbss", "rxnobuf", "rxf0ovfl",
       "rxf1ovfl", "rxf2ovfl", "rxoflo", "rxuflo[0]", "rxuflo[1]", "rxuflo[2]", "rxerror",
       "rxbadfcs", "rxbadplcp", "rxcrsglitch", "rxstrt", "rxundec", "ccmpundec", "ccmpreplay",
       "rxdup", "dmade", "dmada", "dmape", "dma_hang", "reset", "reinit", "phywatchdog",
       "psmwds", "pciereset", "txframe", "txfail", "txnoack", "txretry", "txnobuf", "txerror",
       "pmqovfl", "rxrsptmout", "rfdisable"]

BCME = dict(enumerate("""OK ERROR BADARG BADOPTION NOTUP NOTDOWN NOTAP NOTSTA BADKEYIDX RADIOOFF
NOTBANDLOCKED NOCLK BADRATESET BADBAND BUFTOOSHORT BUFTOOLONG BUSY NOTASSOCIATED BADSSIDLEN
OUTOFRANGECHAN BADCHAN BADADDR NORESOURCE UNSUPPORTED BADLEN NOTREADY EPERM NOMEM ASSOCIATED
RANGE NOTFOUND WME_NOT_ENABLED TSPEC_NOTFOUND ACM_NOTSUPPORTED NOT_WME_ASSOCIATION SDIO_ERROR
DONGLE_DOWN VERSION TXFAIL RXFAIL NODEVICE NMODE_DISABLED NONRESIDENT SCANREJECT""".split()))

def last_bcmerror(ifc):     # the driver keeps the last Broadcom error code (root only)
    buf = ctypes.create_string_buffer(b"bcmerror\0", BUFLEN)
    ioc = WlIoctl(WLC_GET_VAR, ctypes.addressof(buf), BUFLEN, 0, 0, 0)
    ifr = struct.pack("16sP", ifc.encode(), ctypes.addressof(ioc)) + b"\0" * 16
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            fcntl.ioctl(s.fileno(), SIOCDEVPRIVATE, ifr)
        v = struct.unpack_from("<i", buf.raw, 0)[0]
        return f" (BCME_{BCME.get(-v, v)})"
    except OSError:
        return ""

class WlIoctl(ctypes.Structure):
    _fields_ = [("cmd", ctypes.c_uint), ("buf", ctypes.c_void_p), ("len", ctypes.c_uint),
                ("set", ctypes.c_uint8), ("used", ctypes.c_uint), ("needed", ctypes.c_uint)]

def wl_iface():
    for d in sorted(os.listdir("/sys/class/net")):
        try:
            if os.path.basename(os.readlink(f"/sys/class/net/{d}/device/driver")) == "wl":
                return d
        except OSError:
            pass
    sys.exit("no interface driven by 'wl' found")

def ioctl_works(ifc):
    """True if the driver answers private ioctls at all ("counters" always exists)."""
    buf = ctypes.create_string_buffer(b"counters\0", BUFLEN)
    ioc = WlIoctl(WLC_GET_VAR, ctypes.addressof(buf), len(buf), 0, 0, 0)
    ifr = struct.pack("16sP", ifc.encode(), ctypes.addressof(ioc)) + b"\0" * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            fcntl.ioctl(s.fileno(), SIOCDEVPRIVATE, ifr)
            return True
        except OSError:
            return False

def wl_ioctl(ifc, cmd, buf, setflag, what):
    ioc = WlIoctl(cmd, ctypes.addressof(buf), len(buf), setflag, 0, 0)
    ifr = struct.pack("16sP", ifc.encode(), ctypes.addressof(ioc)) + b"\0" * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            fcntl.ioctl(s.fileno(), SIOCDEVPRIVATE, ifr)
        except OSError as e:
            if e.errno == 95 and not ioctl_works(ifc):
                sys.exit("driver does not accept private ioctls on this kernel "
                         "(apply patches/0002-wl-restore-private-ioctl-on-5.15plus.patch)")
            if e.errno == 95:       # the blob's BCME_UNSUPPORTED maps to EOPNOTSUPP
                sys.exit(f"ioctl {what}: not supported by the driver (BCME_UNSUPPORTED)")
            sys.exit(f"ioctl {what}: {e}{last_bcmerror(ifc) if os.geteuid() == 0 else ''}")
    return buf.raw

def iovar_get(ifc, name, arg=b""):
    buf = ctypes.create_string_buffer(name.encode() + b"\0" + arg, BUFLEN)
    return wl_ioctl(ifc, WLC_GET_VAR, buf, 0, name)

def iovar_set_int(ifc, name, val):      # needs root (the driver refuses sets from users)
    buf = ctypes.create_string_buffer(name.encode() + b"\0" + struct.pack("<i", val))
    wl_ioctl(ifc, WLC_SET_VAR, buf, 1, name)

def updown(ifc, up):                    # needs root; 'down' stops the radio until 'up'
    buf = ctypes.create_string_buffer(4)
    wl_ioctl(ifc, WLC_UP if up else WLC_DOWN, buf, 1, "up" if up else "down")

def counters(ifc):
    raw = iovar_get(ifc, "counters")
    ver, length = struct.unpack_from("<HH", raw, 0)
    out = {"version": ver, "length": length}
    off = 4
    for f in CNT_FIELDS:
        if "[" in f:
            base, n = f[:-1].split("[")
            for i in range(int(n)):
                out[f"{base}[{i}]"] = struct.unpack_from("<I", raw, off)[0]; off += 4
        else:
            out[f] = struct.unpack_from("<I", raw, off)[0]; off += 4
    if ver != 10:
        print(f"warning: driver reports counters version {ver}, tool expects 10; "
              "field names may be shifted", file=sys.stderr)
    return out

def read_dbg():
    try:
        with open("/proc/wl_dbg") as f:
            return dict(l.split(None, 1) for l in f.read().split("\n") if l.strip())
    except OSError:
        return None

def netstat(ifc, name):
    try:
        with open(f"/sys/class/net/{ifc}/statistics/{name}") as f:
            return int(f.read())
    except OSError:
        return 0

def irq_count(ifc):
    try:
        with open("/proc/interrupts") as f:
            for line in f:
                if ifc in line or " wl" in line.split(":", 1)[-1][-30:]:
                    parts = line.split()
                    return sum(int(x) for x in parts[1:] if x.isdigit())
    except OSError:
        pass
    return 0

def dma_state(word):
    st = {0: "dis", 1: "act", 2: "idl", 3: "stp", 4: "sus"}.get(word >> 28, "?")
    return st

def record(ifc, iv):
    """One line per interval. Deltas unless marked =. A stall is RX packets
    flat for >= 3 intervals while TX keeps moving."""
    have_cnt = True
    try:
        prev_c = counters(ifc)
    except SystemExit as e:
        print(f"# firmware counters unavailable: {e}", file=sys.stderr)
        have_cnt, prev_c = False, {}
    prev_d = read_dbg() or {}
    prev = (netstat(ifc, "rx_packets"), netstat(ifc, "tx_packets"), irq_count(ifc))
    flat = 0
    hdr = ["time", "rxpk", "txpk", "irq"]
    if prev_d:
        hdr += ["isr", "ours", "wdpc", "dpc", "resch", "intron", "sendup", "pfail",
                "ms_ours", "ms_send", "rs", "macint", "intmask", "rx0cur", "rx0last", "rx0st", "rx0err"]
    if have_cnt:
        hdr += ["rxframe", "ucast", "bcn", "nobuf", "f0ovfl", "rxoflo", "dmaerr", "reset", "badfcs", "crsgl"]
    print(" ".join(hdr), flush=True)
    while True:
        time.sleep(iv)
        cur = (netstat(ifc, "rx_packets"), netstat(ifc, "tx_packets"), irq_count(ifc))
        row = [time.strftime("%H:%M:%S")] + [str(c - p) for c, p in zip(cur, prev)]
        d = read_dbg() or {}
        if prev_d and d:
            def dd(k):
                try:
                    return str((int(d[k]) - int(prev_d[k])) & 0xffffffff)
                except (KeyError, ValueError):
                    return "-"
            row += [dd(k) for k in ("isr_calls", "isr_ours", "isr_wantdpc", "dpc_runs",
                                    "dpc_resched", "dpc_intrson", "sendup", "pktget_fail")]
            row += [d.get("ms_since_ours", "-").strip(), d.get("ms_since_sendup", "-").strip(),
                    d.get("resched", "-").strip(), d.get("macintstatus", "-").strip()[2:],
                    d.get("macintmask", "-").strip()[2:]]
            rx = d.get("dma0_rx", "").split()
            if len(rx) == 6:
                ptr, s0, s1 = int(rx[1], 16), int(rx[4], 16), int(rx[5], 16)
                row += [f"{s0 & 0x1fff:x}", f"{ptr & 0x1fff:x}", dma_state(s0), f"{s1 >> 28:x}"]
            else:
                row += ["-"] * 4
        if have_cnt:
            try:
                c = counters(ifc)
                row += [str((c[k] - prev_c[k]) & 0xffffffff) for k in
                        ("rxframe", "rxdfrmucastmbss", "rxbeaconmbss", "rxnobuf", "rxf0ovfl", "rxoflo")]
                row.append(str(sum((c[k] - prev_c[k]) & 0xffffffff for k in ("dmade", "dmada", "dmape", "dma_hang"))))
                row += [str((c[k] - prev_c[k]) & 0xffffffff) for k in ("reset", "rxbadfcs", "rxcrsglitch")]
                prev_c = c
            except SystemExit:
                row += ["?"] * 10
        flat = flat + 1 if cur[0] == prev[0] and cur[1] != prev[1] else 0
        if flat >= 3:
            row.append("<<STALL")
        print(" ".join(row), flush=True)
        prev, prev_d = cur, d

def main(a):
    cmd = a[0] if a else "counters"
    ifc = os.environ.get("WL_IFACE") or wl_iface()
    if cmd == "counters":
        c = counters(ifc)
        if "--json" in a:
            print(json.dumps(c)); return
        for k, v in c.items():
            if v:
                print(f"{k:24} {v}")
    elif cmd == "watch":
        iv = float(a[1]) if len(a) > 1 else 1.0
        prev = counters(ifc)
        cols = [k for k in KEY if k in prev]
        print("time     " + " ".join(f"{k[:9]:>9}" for k in cols), flush=True)
        while True:
            time.sleep(iv)
            cur = counters(ifc)
            print(time.strftime("%H:%M:%S ") +
                  " ".join(f"{(cur[k] - prev[k]) & 0xffffffff:>9}" for k in cols), flush=True)
            prev = cur
    elif cmd == "get":
        raw = iovar_get(ifc, a[1])
        v = struct.unpack_from("<i", raw, 0)[0]
        print(f"{a[1]} = {v} (0x{v & 0xffffffff:08x})  raw: {raw[:32].hex()}")
    elif cmd == "set":
        iovar_set_int(ifc, a[1], int(a[2], 0))
        v = struct.unpack_from("<i", iovar_get(ifc, a[1]), 0)[0]
        print(f"{a[1]} = {v}")
    elif cmd in ("up", "down"):
        updown(ifc, cmd == "up")
    elif cmd == "getstr":
        raw = iovar_get(ifc, a[1], (a[2].encode() + b"\0") if len(a) > 2 else b"")
        print(raw.split(b"\0", 1)[0].decode(errors="replace"))
    elif cmd == "record":
        record(ifc, float(a[1]) if len(a) > 1 else 1.0)
    elif cmd == "dbg":
        d = read_dbg()
        if d is None:
            sys.exit("/proc/wl_dbg not present (only on instrumented debug builds)")
        for k, v in d.items():
            print(f"{k:24} {v.strip()}")
    else:
        print(__doc__); sys.exit(2)

if __name__ == "__main__":
    main(sys.argv[1:])
