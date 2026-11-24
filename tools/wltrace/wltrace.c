// SPDX-License-Identifier: GPL-2.0
/* wltrace: separate debug module (GPL, so it may use kprobes) that watches the
 * Broadcom wl driver from outside. Never shipped with the fix. Load after wl.
 * ---- rxtrace -----------------------------------------------------------------
 * Catches the moment the closed binary stores a CCMP receive counter that jumps
 * far ahead, and records the frame that did it plus the frames just before.
 * kprobes on blob functions (offsets for wlc_hybrid.o_amd64 6.30.223.271):
 *   wlc_recvdata_ordered (wlc, scb, f)   entry: remember who called it
 *   wlc_wsec_rxiv_update (wsec, f)       entry: f+0x50 key, f+0x80 slot,
 *                                        f+0x82 PN lo16, f+0x84 PN hi32; it stores PN+1
 *   wlc_wsec_sw_encrypt_data+0x405       CCMP replay branch: r14 = PN hi32,
 *                                        [rbp-0x172] = PN lo16, rbx = f
 * f (wlc_frminfo): +0x00 802.11 header, +0x08 body/IV pointer, +0x20 packet,
 *   +0x28 d11 rx header, +0x30 frame control, +0x36 TID, +0x40 multicast,
 *   +0x41 A-MSDU present, +0x48 protected, +0x88 flags, +0xae sequence control.
 *   wlc_recv+0x41 (0x4c17f)              every received buffer (each A-MSDU piece):
 *                                        r14 = d11 rx header, rbx = wlc, [rbp-0x70] = packet;
 *                                        wlc+0x178 = A-MSDU info (+0x68 state, +0x80 counters)
 * v2: at a jump, also the head piece's own rx header (bytes before the 802.11 header) and
 *   the received pieces around each piece of the chain (matched by buffer, never printed).
 * Only frame headers, IV bytes and hardware status are kept; no key bytes.
 * Read with: cat /proc/wl_rxtrace
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/skbuff.h>
#include <linux/seq_file.h>
#include <linux/proc_fs.h>
#include <linux/vmalloc.h>
#include <linux/percpu.h>
#include <linux/kprobes.h>
#include <linux/uaccess.h>
#include <linux/sched/clock.h>

#define RT_RING 32
#define RT_TRIG 24
#define RT_REPLAYS 8
struct rt_ev {
	u64 t;
	u64 newpn, oldpn;
	u32 ret, rdo_ret;
	u8 slot, cpu, nchain, hdr_ok;
	u16 iv_off;
	u32 skblen;
	u8 f30[0x20];		/* f+0x30..0x4f: fc, TID, flags, protected */
	u16 f88, seq;
	u8 hdr[48];
	u8 rxh[32];
};
struct rt_rep { u64 t; u64 pn; u16 seq; u8 slot; u8 amsdu; };
#define PC_RING 4096
#define PC_WIN 40
#define RT_CH 8
struct pc_ev {			/* one received buffer, as wlc_recv sees it */
	u64 t;
	const void *skb;	/* only for matching, never printed */
	u32 len, errc;
	u16 rxhlen;
	u8 cpu, state;
	u8 raw[96];		/* d11 rx header, pad, PLCP, start of frame */
};
struct pc_win { int n, from; struct pc_ev e[PC_WIN]; };
struct rt_trig {
	struct rt_ev ev;
	struct rt_ev ring[RT_RING];
	int nring, nrep;
	const void *ch[RT_CH];		/* chain members, for matching only */
	u32 chlen[RT_CH];
	u8 pre[64];			/* 64 bytes before the head's 802.11 header */
	struct pc_win win[3];		/* pieces around head, around last member, newest */
	int age[RT_CH];			/* pieces received since each member (-1 = not found) */
	u64 pc_now;
	struct rt_rep rep[RT_REPLAYS];
};

static DEFINE_RAW_SPINLOCK(rt_lock);
static struct rt_ev rt_ring[RT_RING];
static unsigned int rt_pos;
static struct rt_trig *rt_trig;		/* RT_TRIG entries */
static int rt_ntrig;
static u64 rt_calls, rt_replays, rt_lost;
static DEFINE_PER_CPU(unsigned long, rt_rdo_ret);
static unsigned long rt_base;		/* blob .text start, so offsets match objdump */
static int rt_on;
static struct pc_ev *pc_ring;		/* PC_RING entries */
static u64 pc_n;			/* total pieces seen */
static u64 pc_fcs[4], pc_agg[4];	/* FCS-error pieces / all A-MSDU pieces, by aggtype */
static void pc_snap(struct rt_trig *tg);

static inline u32 rt_off(unsigned long a) { return (u32)(a - rt_base); }
static inline void rt_rd(void *dst, const void *src, size_t n)
{
	if (copy_from_kernel_nofault(dst, src, n))
		memset(dst, 0xee, n);
}

static int rt_rdo_pre(struct kprobe *p, struct pt_regs *regs)
{
	unsigned long ra = 0;
	rt_rd(&ra, (void *)regs->sp, sizeof(ra));
	this_cpu_write(rt_rdo_ret, ra);
	return 0;
}

/* copy up to PC_WIN pieces: the one received 'age' pieces ago, 'after' newer ones and
 * older ones before it; caller holds rt_lock */
static void pc_copy(struct pc_win *w, u64 age, u64 after)
{
	u64 end = pc_n, last, first;
	int k;
	w->n = 0;
	w->from = 0;
	if (!end)
		return;
	if (age > end)
		age = end;
	last = end - age + after;		/* newest index to copy */
	if (last >= end)
		last = end - 1;
	first = last + 1 >= PC_WIN ? last + 1 - PC_WIN : 0;
	if (end - first > PC_RING)
		first = end - PC_RING;
	w->from = (int)(end - first);		/* age of the first copied piece */
	for (k = 0; k < PC_WIN && first + k <= last; k++)
		w->e[w->n++] = pc_ring[(first + k) % PC_RING];
}

static void pc_snap(struct rt_trig *tg)
{
	int i;
	u64 k, lim = pc_n < PC_RING ? pc_n : PC_RING;
	for (i = 0; i < RT_CH; i++) {
		tg->age[i] = -1;
		if (!tg->ch[i])
			continue;
		for (k = 1; k <= lim; k++)
			if (pc_ring[(pc_n - k) % PC_RING].skb == tg->ch[i]) {
				tg->age[i] = (int)k;
				break;
			}
	}
	tg->pc_now = pc_n;
	memset(tg->win, 0, sizeof(tg->win));
	if (tg->age[0] > 0)
		pc_copy(&tg->win[0], tg->age[0], 6);		/* head piece and what came before */
	for (i = RT_CH - 1; i > 0; i--)
		if (tg->ch[i] && tg->age[i] > 0) {
			pc_copy(&tg->win[1], tg->age[i], 6);	/* last chain member found */
			break;
		}
	pc_copy(&tg->win[2], 1, 0);			/* the newest pieces */
}

static int pc_pre(struct kprobe *p, struct pt_regs *regs)
{
	const u8 *rxh = (const u8 *)regs->r14, *wlc = (const u8 *)regs->bx;
	const u8 *info = NULL, *cnt = NULL;
	struct pc_ev e;
	unsigned long flags;
	u32 st = 0;
	u16 s1 = 0, s2 = 0;

	if (!pc_ring)
		return 0;
	e.t = local_clock();
	e.skb = NULL;
	rt_rd(&e.skb, (void *)(regs->bp - 0x70), sizeof(e.skb));
	e.len = 0;
	if (e.skb)
		rt_rd(&e.len, &((const struct sk_buff *)e.skb)->len, 4);
	e.rxhlen = 0;
	rt_rd(&e.rxhlen, wlc + 0x6bc, 2);
	e.cpu = smp_processor_id();
	rt_rd(e.raw, rxh, sizeof(e.raw));
	rt_rd(&info, wlc + 0x178, sizeof(info));
	e.errc = 0;
	if (info) {
		rt_rd(&st, info + 0x68, 4);
		rt_rd(&cnt, info + 0x80, sizeof(cnt));
		if (cnt)
			rt_rd(&e.errc, cnt + 0x38, 4);
	}
	e.state = (u8)st;
	memcpy(&s1, e.raw + 0x10, 2);
	memcpy(&s2, e.raw + 0x12, 2);
	raw_spin_lock_irqsave(&rt_lock, flags);
	pc_ring[pc_n % PC_RING] = e;
	pc_n++;
	if (s2 & 1) {
		pc_agg[(s2 >> 1) & 3]++;
		if (s1 & 1)
			pc_fcs[(s2 >> 1) & 3]++;
	}
	raw_spin_unlock_irqrestore(&rt_lock, flags);
	return 0;
}

static int rt_upd_pre(struct kprobe *p, struct pt_regs *regs)
{
	const u8 *f = (const u8 *)regs->si;
	const u8 *key = NULL, *hdr = NULL, *body = NULL, *rxh = NULL;
	struct sk_buff *skb = NULL, *nx;
	struct rt_ev e;
	unsigned long ra = 0, flags;
	u32 hi = 0, ohi = 0; u16 lo = 0, olo = 0;
	int i;

	memset(&e, 0, sizeof(e));
	e.t = local_clock();
	rt_rd(&key, f + 0x50, sizeof(key));
	rt_rd(&e.slot, f + 0x80, 1);
	rt_rd(&lo, f + 0x82, 2);
	rt_rd(&hi, f + 0x84, 4);
	e.newpn = (((u64)hi << 16) | lo) + 1;		/* what the blob is about to store */
	if (key && e.slot < 4) {
		rt_rd(&ohi, key + 0x34 + 8 * e.slot, 4);
		rt_rd(&olo, key + 0x38 + 8 * e.slot, 2);
	}
	e.oldpn = ((u64)ohi << 16) | olo;
	rt_rd(&ra, (void *)regs->sp, sizeof(ra));
	e.ret = rt_off(ra);
	e.rdo_ret = rt_off(this_cpu_read(rt_rdo_ret));
	e.cpu = smp_processor_id();
	rt_rd(e.f30, f + 0x30, sizeof(e.f30));
	rt_rd(&e.f88, f + 0x88, 2);
	rt_rd(&e.seq, f + 0xae, 2);
	rt_rd(&hdr, f + 0x00, sizeof(hdr));
	rt_rd(&body, f + 0x08, sizeof(body));
	rt_rd(&rxh, f + 0x28, sizeof(rxh));
	rt_rd(&skb, f + 0x20, sizeof(skb));
	if (hdr) {
		rt_rd(e.hdr, hdr, sizeof(e.hdr));
		e.hdr_ok = 1;
		e.iv_off = (u16)(body - hdr);
	}
	if (rxh)
		rt_rd(e.rxh, rxh, sizeof(e.rxh));
	{
	const void *ch[RT_CH] = { 0 }; u32 chlen[RT_CH] = { 0 }; u8 pre[64];
	for (nx = skb, i = 0; nx && i < 64; i++) {
		struct sk_buff *n2 = NULL;
		if (i == 0)
			rt_rd(&e.skblen, &nx->len, 4);
		if (i < RT_CH) {
			ch[i] = nx;
			rt_rd(&chlen[i], &nx->len, 4);
		}
		rt_rd(&n2, &nx->next, sizeof(n2));
		nx = n2;
	}
	e.nchain = i;
	if (hdr)
		rt_rd(pre, hdr - sizeof(pre), sizeof(pre));
	else
		memset(pre, 0xee, sizeof(pre));

	raw_spin_lock_irqsave(&rt_lock, flags);
	rt_calls++;
	if (e.newpn > e.oldpn + 0x8000) {
		if (rt_ntrig < RT_TRIG) {
			struct rt_trig *tg = &rt_trig[rt_ntrig++];
			unsigned int k;
			tg->ev = e;
			tg->nring = 0;
			tg->nrep = 0;
			for (k = 0; k < RT_RING; k++) {
				const struct rt_ev *r = &rt_ring[(rt_pos + k) % RT_RING];
				if (r->t)
					tg->ring[tg->nring++] = *r;
			}
			memcpy(tg->ch, ch, sizeof(ch));
			memcpy(tg->chlen, chlen, sizeof(chlen));
			memcpy(tg->pre, pre, sizeof(pre));
			pc_snap(tg);
		} else
			rt_lost++;
	}
	rt_ring[rt_pos] = e;
	rt_pos = (rt_pos + 1) % RT_RING;
	raw_spin_unlock_irqrestore(&rt_lock, flags);
	}
	return 0;
}

static int rt_rep_pre(struct kprobe *p, struct pt_regs *regs)
{
	const u8 *f = (const u8 *)regs->bx;
	u16 lo = 0, seq = 0; u8 slot = 0, amsdu = 0;
	unsigned long flags;

	rt_rd(&lo, (void *)(regs->bp - 0x172), 2);
	rt_rd(&seq, f + 0xae, 2);
	rt_rd(&slot, f + 0x80, 1);
	rt_rd(&amsdu, f + 0x41, 1);
	raw_spin_lock_irqsave(&rt_lock, flags);
	rt_replays++;
	if (rt_ntrig) {
		struct rt_trig *tg = &rt_trig[rt_ntrig - 1];
		if (tg->nrep < RT_REPLAYS) {
			struct rt_rep *r = &tg->rep[tg->nrep++];
			r->t = local_clock();
			r->pn = ((u64)(u32)regs->r14 << 16) | lo;
			r->seq = seq; r->slot = slot; r->amsdu = amsdu;
		}
	}
	raw_spin_unlock_irqrestore(&rt_lock, flags);
	return 0;
}

static struct kprobe rt_kp_rdo = { .symbol_name = "wlc_recvdata_ordered", .pre_handler = rt_rdo_pre };
static struct kprobe rt_kp_upd = { .symbol_name = "wlc_wsec_rxiv_update", .pre_handler = rt_upd_pre };
static struct kprobe rt_kp_rep = { .symbol_name = "wlc_wsec_sw_encrypt_data", .offset = 0x405,
				   .pre_handler = rt_rep_pre };
static struct kprobe rt_kp_pc = { .symbol_name = "wlc_recv", .offset = 0x41, .pre_handler = pc_pre };

static int rt_start(void)
{
	int e1, e2, e3, e4;
	if (!(pc_ring = vzalloc(sizeof(*pc_ring) * PC_RING)) ||
	    !(rt_trig = vzalloc(sizeof(*rt_trig) * RT_TRIG))) {
		vfree(pc_ring);
		pc_ring = NULL;
		printk(KERN_ERR "wl: rxtrace: no memory\n");
		return -ENOMEM;
	}
	e1 = register_kprobe(&rt_kp_rdo);
	e2 = register_kprobe(&rt_kp_upd);
	e3 = register_kprobe(&rt_kp_rep);
	e4 = register_kprobe(&rt_kp_pc);
	if (!e1)	/* blob .text start, so printed offsets match objdump of the blob */
		rt_base = (unsigned long)rt_kp_rdo.addr - 0x3ecbc;
	printk(KERN_INFO "wl: rxtrace: kprobes %d %d %d %d at +%x +%x +%x +%x\n", e1, e2, e3, e4,
	       e1 ? 0 : rt_off((unsigned long)rt_kp_rdo.addr),
	       e2 ? 0 : rt_off((unsigned long)rt_kp_upd.addr),
	       e3 ? 0 : rt_off((unsigned long)rt_kp_rep.addr),
	       e4 ? 0 : rt_off((unsigned long)rt_kp_pc.addr));
	/* only trust the probes if they landed exactly where the disassembly says */
	if (e1 || e2 || e3 || e4 || rt_off((unsigned long)rt_kp_rdo.addr) != 0x3ecbc ||
	    rt_off((unsigned long)rt_kp_pc.addr) != 0x4c17f ||
	    rt_off((unsigned long)rt_kp_upd.addr) != 0x171768 ||
	    rt_off((unsigned long)rt_kp_rep.addr) != 0x17252e) {
		printk(KERN_ERR "wl: rxtrace: probe addresses unexpected, disabled\n");
		if (!e1) unregister_kprobe(&rt_kp_rdo);
		if (!e2) unregister_kprobe(&rt_kp_upd);
		if (!e3) unregister_kprobe(&rt_kp_rep);
		if (!e4) unregister_kprobe(&rt_kp_pc);
		vfree(rt_trig);
		vfree(pc_ring);
		pc_ring = NULL;
		return -ENOENT;
	}
	rt_on = 1;
	return 0;
}

static void rt_stop(void)
{
	if (rt_on) {
		unregister_kprobe(&rt_kp_pc);
		unregister_kprobe(&rt_kp_rep);
		unregister_kprobe(&rt_kp_upd);
		unregister_kprobe(&rt_kp_rdo);
		rt_on = 0;
	}
	vfree(rt_trig);
	rt_trig = NULL;
	vfree(pc_ring);
	pc_ring = NULL;
}

static void pc_print_win(struct seq_file *m, const char *name, const struct rt_trig *tg, const struct pc_win *w)
{
	int k, i;
	seq_printf(m, "  pieces %s (age = buffers received since, 1 = newest):\n", name);
	for (k = 0; k < w->n; k++) {
		const struct pc_ev *e = &w->e[k];
		char tag[3] = "  ";
		for (i = 0; i < RT_CH; i++)
			if (tg->ch[i] && e->skb == tg->ch[i]) {
				tag[0] = 'm'; tag[1] = '0' + i;
			}
		seq_printf(m, "   %s age=%d t=%llu.%06llu cpu%u len=%u st=%u err=%u rxhlen=%u raw %*phN\n",
			   tag, w->from - k, e->t / 1000000000ULL, (e->t / 1000) % 1000000ULL, e->cpu,
			   e->len, e->state, e->errc, e->rxhlen, (int)sizeof(e->raw), e->raw);
	}
}

static void rt_print_ev(struct seq_file *m, const char *tag, const struct rt_ev *e)
{
	seq_printf(m, "%s t=%llu.%06llu cpu%u ret=%x rdo=%x slot=%u old=%llu new=%llu (+%lld) "
		   "seq=%u/%u f88=%04x iv_off=%u len=%u chain=%u\n",
		   tag, e->t / 1000000000ULL, (e->t / 1000) % 1000000ULL, e->cpu, e->ret, e->rdo_ret,
		   e->slot, e->oldpn, e->newpn, (long long)(e->newpn - e->oldpn),
		   e->seq >> 4, e->seq & 0xf, e->f88, e->iv_off, e->skblen, e->nchain);
	seq_printf(m, "    f30 %*phN\n    hdr %*phN\n    rxh %*phN\n",
		   (int)sizeof(e->f30), e->f30, (int)sizeof(e->hdr), e->hdr, (int)sizeof(e->rxh), e->rxh);
}

static int wl_rxtrace_show(struct seq_file *m, void *v)
{
	struct rt_trig *copy;
	int n, i, j;
	u64 calls, reps, lost;
	unsigned long flags;

	if (!rt_on) {
		seq_puts(m, "rxtrace off (see dmesg)\n");
		return 0;
	}
	if (!(copy = vmalloc(sizeof(*copy) * RT_TRIG)))
		return -ENOMEM;
	raw_spin_lock_irqsave(&rt_lock, flags);
	n = rt_ntrig;
	memcpy(copy, rt_trig, sizeof(*copy) * n);
	calls = rt_calls; reps = rt_replays; lost = rt_lost;
	raw_spin_unlock_irqrestore(&rt_lock, flags);
	seq_printf(m, "rxiv_update calls %llu, replay drops %llu, triggers %d (+%llu not kept)\n",
		   calls, reps, n, lost);
	seq_printf(m, "buffers %llu; A-MSDU pieces mid/first/last/one %llu/%llu/%llu/%llu, "
		   "with FCS error %llu/%llu/%llu/%llu\n", pc_n, pc_agg[0], pc_agg[1], pc_agg[2], pc_agg[3],
		   pc_fcs[0], pc_fcs[1], pc_fcs[2], pc_fcs[3]);
	for (i = 0; i < n; i++) {
		seq_printf(m, "\n=== trigger %d\n", i + 1);
		for (j = 0; j < copy[i].nring; j++)
			rt_print_ev(m, "  before", &copy[i].ring[j]);
		rt_print_ev(m, "  JUMP  ", &copy[i].ev);
		seq_printf(m, "  chain:");
		for (j = 0; j < RT_CH && copy[i].ch[j]; j++)
			seq_printf(m, " m%d len=%u age=%d", j, copy[i].chlen[j], copy[i].age[j]);
		seq_printf(m, "\n  pre %*phN\n", 64, copy[i].pre);
		pc_print_win(m, "around head (m0)", &copy[i], &copy[i].win[0]);
		pc_print_win(m, "around last member found", &copy[i], &copy[i].win[1]);
		pc_print_win(m, "newest", &copy[i], &copy[i].win[2]);
		for (j = 0; j < copy[i].nrep; j++) {
			const struct rt_rep *r = &copy[i].rep[j];
			seq_printf(m, "  replay t=%llu.%06llu pn=%llu seq=%u/%u slot=%u amsdu=%u\n",
				   r->t / 1000000000ULL, (r->t / 1000) % 1000000ULL, r->pn,
				   r->seq >> 4, r->seq & 0xf, r->slot, r->amsdu);
		}
	}
	vfree(copy);
	return 0;
}


static int __init wltrace_init(void)
{
	int err = rt_start();
	if (err)
		return err;
	if (!proc_create_single("wl_rxtrace", 0444, NULL, wl_rxtrace_show)) {
		rt_stop();
		return -ENOMEM;
	}
	return 0;
}

static void __exit wltrace_exit(void)
{
	remove_proc_entry("wl_rxtrace", NULL);
	rt_stop();
}

module_init(wltrace_init);
module_exit(wltrace_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("debug: trace the wl blob's CCMP receive counter updates");
