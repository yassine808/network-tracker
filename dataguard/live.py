"""Live monitoring: sampling counters, the 15-minute ring, alerts and the dashboard status."""

import collections
import logging
import threading
import time
from datetime import date, datetime, timedelta

from .common import psutil
from .interfaces import auto_iface, get_ssid
from .notify import notify
from .processes import top_processes

# A sample lasts 1-5 s: even 2.5 Gbps stays under 1.6 GB. Windows occasionally hands back a
# counter that doubled - the jump looks like real traffic unless it is rejected here.
COUNTER_JUMP = 2 * 2 ** 30

KILL_GB = 10  # hard daily stop: past this on the configured hotspot, the internet gets cut

WARN_KINDS = ("level", "daily", "pace", "burst")  # alert kinds the dashboard shows as a warning veil


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
        self.t_kill = 0.0
        self.kill_state = None  # None = not yet synced with the firewall (True/False after check_kill)
        self.err = ""
        self.stop = threading.Event()
        self.wake = threading.Event()     # lets a freshly opened dashboard end the slow idle sleep
        psutil.cpu_percent(interval=None)  # start the CPU meter: first call only sets the baseline
        self.proc = psutil.Process()        # this app's own process: its CPU goes in the sidebar
        self.proc.cpu_percent(interval=None)  # first call only sets the per-process baseline

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
            if prev is not None:
                fwd = tuple(a - b for a, b in zip(cur, prev))
                if min(fwd) >= 0:
                    if max(fwd) <= COUNTER_JUMP:
                        delta[nic] = fwd
                    else:
                        logging.warning("Implausible counter jump on %s ignored (prev=%s cur=%s)", nic, prev, cur)
                elif max(cur) > COUNTER_JUMP:  # back near an old huge value: the twin of a doubled reading
                    logging.warning("Implausible counter re-read on %s ignored (prev=%s cur=%s)", nic, prev, cur)
                else:
                    delta[nic] = cur  # a counter that went backwards was reset (adapter restart): count from zero
            self.base[nic] = cur
        t0, self.t_last = self.t_last, now
        if t0 is None or now <= t0 or self.iface not in delta:
            return
        drx, dtx = delta[self.iface]

        if cfg["ssid"]:
            big = drx + dtx >= 5_000_000
            if now - self.ssid_t >= self.SSID_EVERY or (big and now - self.ssid_t >= 3):
                self.ssid, self.ssid_t = get_ssid(), now
            # a hotspot is configured: only that exact network counts, an unknown or different
            # name counts as nothing (the dashboard shows "Not counted on this network")
            counted = bool(self.ssid) and self.ssid == cfg["ssid"]
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
            if self.counting:  # off the configured network: no alerts, no projections, nothing calculated
                self.check_alerts(datetime.fromtimestamp(now))
        self.check_kill(now)
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
        st.add_alert(kind, title, msg)
        logging.info("%s - %s", title, msg)
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
                    out.append(("limit", "Data plan used up",
                                f"{g(s['used'])} of {g(s['plan'])} used, {s['days_left']} days to renewal. "
                                "Pause big downloads and updates."))
                else:
                    out.append(("level", f"{top}% of your data used",
                                f"{g(s['used'])} of {g(s['plan'])}. {s['days_left']} days to renewal - "
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

    # -- the 10 GB hard stop
    def check_kill(self, now, force=False):
        """Cut this PC's internet once usage today passes KILL_GB - but only while the configured
        hotspot (exact SSID match) is the Wi-Fi actually in use. Any other network, or an unknown
        name, or no SSID configured at all: the app just watches and never touches the firewall."""
        st, cfg = self.store, self.store.cfg
        if not force and now - self.t_kill < 60:
            return
        self.t_kill = now
        from . import firewall  # local: only Windows has the firewall helper
        try:
            if self.kill_state is None:
                self.kill_state = firewall.cut_active()  # pick up a rule left over from a crash
            ssid, want = self.ssid or "", cfg["ssid"]
            on = bool(want) and bool(ssid) and ssid == want
            off = bool(want) and bool(ssid) and ssid != want
            over = st.summary(datetime.fromtimestamp(now))["today"] >= KILL_GB * st.gb
            if self.kill_state and (not want or off or not over):
                ok, err = firewall.restore_internet()
                if ok:
                    self.kill_state = False
                    if not off:  # left the hotspot: coming back re-cuts; manual restore stays open today
                        with st.lock:
                            st.fired.pop("killday", None)
                            st.dirty = True
                    logging.info("kill-switch: internet restored%s"
                                 % (" (not over the cap anymore)" if not over else ""))
                else:
                    logging.warning("kill-switch: restore failed: %s", err)
            elif not self.kill_state and on and over and st.fired.get("killday") != date.today().isoformat():
                ok, err = firewall.cut_internet()
                if ok:
                    self.kill_state = True
                    with st.lock:
                        st.fired["killday"] = date.today().isoformat()
                        st.dirty = True
                    self.alert("kill", "Internet cut off - 10 GB daily cap reached",
                               f"{st.summary(datetime.fromtimestamp(now))['today'] / st.gb:.1f} GB used today "
                               "on the hotspot. The cap lifts tomorrow, or tap Restore in the dashboard.")
                else:
                    logging.warning("kill-switch: cut failed: %s", err)
        except Exception as e:  # a firewall hiccup must never take the monitor down
            logging.warning("kill-switch: %s", e)

    def skip_kill_today(self):
        """The dashboard's 'cut about to happen' warning was ignored: leave the internet alone today."""
        with self.store.lock:
            self.store.fired["killday"] = date.today().isoformat()
            self.store.dirty = True
            self.store.flush()
        logging.info("kill-switch: automatic cut skipped for today on request")

    def kill_now(self, on):
        """Manual cut/restore from the dashboard. (ok, message)."""
        from . import firewall
        st = self.store
        ok, err = firewall.cut_internet() if on else firewall.restore_internet()
        if ok:
            self.kill_state = bool(on)
            with st.lock:
                if not on:  # an explicit restore is respected for the rest of the day
                    st.fired["killday"] = date.today().isoformat()
                st.dirty = True
            logging.info("kill-switch: internet %s by request", "cut" if on else "restored")
        return ok, err

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

    def ram_mb(self):
        """This app's memory the way Task Manager accounts for it: the host process plus every
        WebView2 child it owns. The host alone reads ~97 MB while the renderers hold ~600 MB."""
        total = self.proc.memory_info().rss
        try:
            kids = self.proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return total / 1048576
        for kid in kids:
            try:
                total += kid.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue  # a renderer that exited between the walk and the read
        return total / 1048576

    def status(self):
        now = time.time()
        was_idle = now - self.last_ui >= 10
        self.last_ui = now  # dashboard is open: sample every second for a while
        if was_idle:
            self.wake.set()  # ...and take a reading now instead of waiting out the 5 s idle sleep
        s = self.store.summary(datetime.fromtimestamp(now))
        ref = self.ring[-1][1] if self.ring else now
        rx, tx = self.window(ref, 4, 1, counted_only=True)   # 4 s average: reacts quickly, still smooth
        srx, stx = self.window(ref, 300, 60, counted_only=True)
        today = date.today()
        with self.store.lock:
            hist = [{"d": (today - timedelta(days=i)).isoformat(),
                     "b": sum(self.store.days.get((today - timedelta(days=i)).isoformat(), [0, 0]))}
                    for i in range(30, -1, -1)]
            fired_kill = self.store.fired.get("killday") == today.isoformat()
            warn_today = any(a.get("kind") in WARN_KINDS and str(a.get("t", ""))[:10] == today.isoformat()
                             for a in self.store.log[-40:])
            s.update(cfg=dict(self.store.cfg), alerts=list(reversed(self.store.log[-10:])))
        # "about to be cut": the daily cap is reached and nothing has dealt with today yet
        pending = bool(s["cfg"].get("ssid")) and s["today"] >= KILL_GB * self.store.gb \
            and not fired_kill and not bool(self.kill_state)
        s.update(iface=self.iface, ssid=self.ssid, counting=self.counting, err=self.err,
                 cpu=psutil.cpu_percent(interval=None), mem=psutil.virtual_memory().percent,
                 app_cpu=self.proc.cpu_percent(interval=None),
                 ram_mb=self.ram_mb(),
                 warn_today=warn_today,
                 kill=dict(active=bool(self.kill_state), pending=pending, gb=KILL_GB, fired=fired_kill),
                 ifaces=sorted(psutil.net_io_counters(pernic=True)),
                 down=rx[0] / 4, up=tx[0] / 4,
                 series_down=[b / 5 for b in srx], series_up=[b / 5 for b in stx], history=hist)
        return s
