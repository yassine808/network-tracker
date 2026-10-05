"""Live monitoring: sampling counters, the 15-minute ring, alerts and the dashboard status."""

import collections
import threading
import time
from datetime import date, datetime, timedelta

from .common import psutil
from .interfaces import auto_iface, get_ssid
from .notify import notify
from .processes import top_processes


class Monitor:
    SSID_EVERY = 30  # seconds between Wi-Fi name checks (extra check during big transfers)

    def __init__(self, store):
        self.store = store
        self.base = {}                    # nic -> (rx, tx) at the previous reading
        self.t_last = None
        self.ring = collections.deque()   # (t0, t1, rx, tx, counted) for the last 15 minutes
        self.iface = None
        self.ssid = None
        self.ssid_t = 0.0
        self.counting = None
        self.last_ui = 0.0
        self.last_burst = 0.0
        self.t_alert = 0.0
        self.t_flush = time.time()
        self.err = ""
        self.stop = threading.Event()
        self.wake = threading.Event()     # lets a freshly opened dashboard end the slow idle sleep

    # -- sampling
    def sample(self, now=None, counters=None, stats=None):
        now = time.time() if now is None else now
        counters = psutil.net_io_counters(pernic=True) if counters is None else counters
        stats = psutil.net_if_stats() if stats is None else stats
        cfg = self.store.cfg
        want = cfg["iface"]
        self.iface = want if want != "auto" and want in counters else auto_iface(list(counters), stats, counters)

        delta = {}
        for nic, c in counters.items():
            cur = (c.bytes_recv, c.bytes_sent)
            prev = self.base.get(nic)
            if prev is not None:  # a counter that went backwards was reset (adapter restart): count from zero
                delta[nic] = tuple(a - b if a >= b else a for a, b in zip(cur, prev))
            self.base[nic] = cur
        t0, self.t_last = self.t_last, now
        if t0 is None or now <= t0 or self.iface not in delta:
            return
        drx, dtx = delta[self.iface]

        if cfg["ssid"]:
            big = drx + dtx >= 5_000_000
            if now - self.ssid_t >= self.SSID_EVERY or (big and now - self.ssid_t >= 3):
                self.ssid, self.ssid_t = get_ssid(), now
            counted = not self.ssid or self.ssid == cfg["ssid"]  # name unknown or unreadable: count rather than miss data
        else:
            counted = True
        self.counting = counted

        with self.store.lock:
            self.ring.append((t0, now, drx, dtx, counted))
            while self.ring and self.ring[0][1] < now - 900:
                self.ring.popleft()
        if counted and (drx or dtx):
            self.store.add(date.fromtimestamp(now).isoformat(), drx, dtx)
            self.check_burst(now)
        if now - self.t_alert >= 10:
            self.t_alert = now
            self.check_alerts(datetime.fromtimestamp(now))
        if now - self.t_flush >= 60:
            self.t_flush = now
            self.store.flush()

    def window(self, ref, span, nbins, counted_only=False):
        """Spread recorded traffic over [ref-span, ref] in equal bins -> (rx_bins, tx_bins) in bytes."""
        w, t_start = span / nbins, ref - span
        rx, tx = [0.0] * nbins, [0.0] * nbins
        with self.store.lock:
            ring = list(self.ring)
        for t0, t1, drx, dtx, counted in ring:
            if t1 <= t_start or t1 <= t0 or (counted_only and not counted):
                continue
            dur, cur = t1 - t0, max(t0, t_start)
            i = min(int((cur - t_start) / w), nbins - 1)
            while cur < t1 and i < nbins:
                seg_end = min(t1, t_start + (i + 1) * w)
                f = (seg_end - cur) / dur
                rx[i] += drx * f
                tx[i] += dtx * f
                cur, i = seg_end, i + 1
        return rx, tx

    # -- alerts
    def alert(self, kind, title, msg):
        st = self.store
        with st.lock:
            st.log.append({"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, "title": title, "msg": msg})
            st.log = st.log[-40:]
            st.dirty = True
        print(f"[{time.strftime('%H:%M:%S')}] {title} - {msg}", flush=True)
        if st.cfg["notify"]:
            notify(title, msg)

    def check_alerts(self, now):
        st = self.store
        s = st.summary(now)
        g = lambda b: f"{b / st.gb:.1f} GB"
        tkey = now.date().isoformat()
        out = []
        with st.lock:
            f = st.fired
            if f.get("cycle") != s["cycle_start"]:
                f.clear()
                f.update(cycle=s["cycle_start"], pcts=[])
                st.dirty = True
            f["pcts"] = [p for p in f.get("pcts", []) if s["pct"] >= p]  # forget levels we've dropped below
            due = [p for p in st.cfg["alert_pcts"] if s["pct"] >= p and p not in f["pcts"]]
            if due:
                top = max(due)
                f["pcts"] = sorted(set(f["pcts"]) | set(due))
                st.dirty = True
                if top >= 100:
                    out.append(("limit", "Data budget used up",
                                f"{g(s['used'])} of {g(s['budget'])} used, {s['days_left']} days to renewal. "
                                "Pause big downloads and updates."))
                else:
                    out.append(("level", f"{top}% of your data used",
                                f"{g(s['used'])} of {g(s['budget'])}. {s['days_left']} days to renewal - "
                                f"about {g(s['allowance'])} a day keeps you on track."))
            if (f.get("daily") != tkey and s["allowance"] > 0 and s["today"] >= s["allowance"] and s["pct"] < 100):
                f["daily"] = tkey
                st.dirty = True
                out.append(("daily", "Today's allowance reached",
                            f"{g(s['today'])} used today (allowance {g(s['allowance'])}). Going over eats into later days."))
            if f.get("pace") != tkey and s["pace_reliable"] and s["run_out"] and s["pct"] < 100:
                f["pace"] = tkey
                st.dirty = True
                out.append(("pace", "On course to run out early",
                            f"At your recent pace the data runs out around {s['run_out']}, "
                            f"{s['days_early']} days before renewal. Aim for {g(s['allowance'])} a day."))
        for kind, title, msg in out:
            self.alert(kind, title, msg)

    def check_burst(self, ref):
        limit = self.store.cfg["burst_mb_per_min"] * self.store.mb
        if not limit or ref - self.last_burst < 600:
            return
        rx, tx = self.window(ref, 60, 1, counted_only=True)
        if rx[0] + tx[0] >= limit:
            self.last_burst = ref
            threading.Thread(target=self._burst_report, args=(rx[0] + tx[0],), daemon=True).start()

    def _burst_report(self, got):
        try:
            names = ", ".join(i["name"] for i in top_processes(window=2.0, limit=3)["items"])
        except Exception:
            names = ""
        who = f" Busiest: {names}." if names else ""
        self.alert("burst", "Heavy download in progress",
                   f"{got / self.store.mb:.0f} MB in the last minute.{who} Open the dashboard for details.")

    # -- loop + status
    def run(self):
        while not self.stop.is_set():
            try:
                self.sample()
                self.err = ""
            except Exception as e:  # keep the guard alive whatever happens
                self.err = f"{type(e).__name__}: {e}"
            self.wake.wait(1.0 if time.time() - self.last_ui < 10 else 5.0)
            self.wake.clear()

    def status(self):
        now = time.time()
        was_idle = now - self.last_ui >= 10
        self.last_ui = now  # dashboard is open: sample every second for a while
        if was_idle:
            self.wake.set()  # ...and take a reading now instead of waiting out the 5 s idle sleep
        s = self.store.summary(datetime.fromtimestamp(now))
        ref = self.ring[-1][1] if self.ring else now
        rx, tx = self.window(ref, 4, 1)  # 4 s average: reacts quickly, still smooth
        srx, stx = self.window(ref, 300, 60)
        today = date.today()
        with self.store.lock:
            hist = [{"d": (today - timedelta(days=i)).isoformat(),
                     "b": sum(self.store.days.get((today - timedelta(days=i)).isoformat(), [0, 0]))}
                    for i in range(30, -1, -1)]
            s.update(cfg=dict(self.store.cfg), alerts=list(reversed(self.store.log[-10:])))
        s.update(iface=self.iface, ssid=self.ssid, counting=self.counting, err=self.err,
                 ifaces=sorted(psutil.net_io_counters(pernic=True)),
                 down=rx[0] / 4, up=tx[0] / 4,
                 series_down=[b / 5 for b in srx], series_up=[b / 5 for b in stx], history=hist)
        return s
