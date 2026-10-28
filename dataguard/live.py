"""Live monitoring: sampling counters, the 15-minute ring, alerts and the dashboard status."""

import collections
import logging
import sys
import threading
import time
from datetime import date, datetime, timedelta

from .common import psutil
from .interfaces import WIFI, auto_iface, get_ssid
from .notify import notify
from .processes import top_processes

# A sample lasts 1-5 s: even 2.5 Gbps stays under 1.6 GB. Windows occasionally hands back a
# counter that doubled - the jump looks like real traffic unless it is rejected here.
# This is only the floor of the ceiling: _frame scales the limit with the actual window, so a
# long sleep or a stalled SSID probe still records the real traffic it moved.
COUNTER_JUMP = 2 * 2 ** 30

KILL_GB = 10  # hard daily stop: past this on the configured hotspot, the internet gets cut

WARN_KINDS = ("level", "daily", "pace", "burst")  # alert kinds the dashboard shows as a warning veil


def _profile_name(ssid):
    """'Meryem 2' -> 'Meryem': Windows appends a number to repeated profiles.

    A linear char scan replaces re.sub(r"\\s+\\d+$") - the quantifier pair backtracks
    quadratically on long digit runs (Sonar S8786)."""
    end = len(ssid)
    i = end
    while i and ssid[i - 1].isdecimal():
        i -= 1
    if i == end or i == 0:
        return ssid  # no trailing digits, or digits with nothing ahead: \s+ needs a space
    j = i
    while j and ssid[j - 1].isspace():
        j -= 1
    if j == i:
        return ssid  # digits not preceded by whitespace: \s+ cannot match
    return ssid[:j]


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
        self.link_up = None               # last seen link state of the counted NIC
        self.link_changed = False         # True on the sample where that link came back up
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
        self.proc = psutil.Process()        # this app's own process: its CPU and RAM go in the chip
        self.proc.cpu_percent(interval=None)  # first call only sets the per-process baseline

    # -- sampling
    def on_network(self, want=None):
        """True when the current network is the configured one, False when it provably isn't.
        The pin is the Wi-Fi name when Windows reveals it; when the name is unreadable the
        pin may also be the interface itself (the dashboard's "Use current Wi-Fi" falls back
        to it), so a hotspot Windows won't name is still pinnable. None = name unreadable on
        a Wi-Fi adapter - can't judge, so trust the adapter (the historical behavior)."""
        want = self.store.cfg["ssid"] if want is None else want
        if not want:
            return True
        ssid = self.ssid or ""
        if ssid:
            # A readable name alone decides: both sides are normalized so a profile saved as
            # "Meryem 2" still matches "Meryem" - in either direction, whichever one the user
            # pinned. A different readable name is a definite "no": the interface fallback
            # below must never rescue it, or every other Wi-Fi network would be counted.
            return ssid == want or _profile_name(ssid) == _profile_name(want)
        if WIFI.search(self.iface or ""):
            # The name is unreadable on a Wi-Fi adapter. If the pin is the interface itself
            # (the dashboard's "Use current Wi-Fi" fallback), trust the adapter; otherwise
            # there is nothing to compare against, so it can't be judged.
            return True if want == self.iface else None
        return False

    def sample(self, now=None, counters=None, stats=None):
        now = time.time() if now is None else now
        counters = psutil.net_io_counters(pernic=True) if counters is None else counters
        stats = psutil.net_if_stats() if stats is None else stats
        cfg = self.store.cfg  # one snapshot for the whole reading, as before the split
        frame = self._frame(cfg, now, counters, stats)
        if frame is None:
            return
        t0, drx, dtx = frame
        was_counting = self.counting
        counted = self._counted(cfg, now, drx, dtx,
                                force=self.wake.is_set() or self.link_changed)
        self._log_counting(cfg, counted)
        # The window that straddles a network change cannot be split between the two
        # networks: crediting it would bill the old network's bytes to the plan (or the
        # reverse). Skip that one window; the next one starts clean.
        straddles = was_counting is not None and counted != was_counting
        credited = counted and not straddles
        if straddles:
            logging.info("monitor: network gate changed during this window; not crediting it "
                         "(ssid=%r configured=%r)", self.ssid, cfg["ssid"])
        logging.debug("sample: %s rx=%d tx=%d counted=%s credited=%s ssid=%s",
                      self.iface, drx, dtx, counted, credited, self.ssid)

        with self.store.lock:
            self.ring.append((t0, now, drx, dtx, credited))
            while self.ring and self.ring[0][1] < now - 900:
                self.ring.popleft()
        if credited and (drx or dtx):
            self._record(t0, now, drx, dtx)
            self.check_burst(now)
        if now - self.t_alert >= 10:
            self.t_alert = now
            if self.counting:  # off the configured network: no alerts, no projections, nothing calculated
                self.check_alerts(datetime.fromtimestamp(now))
        self.check_kill(now)
        if now - self.t_flush >= 60:
            self.t_flush = now
            self.store.flush()

    def _frame(self, cfg, now, counters, stats):
        """Interface pick + counter sanity for one reading -> (t0, rx, tx) or None (not ready)."""
        want = cfg["iface"]
        prev_iface = self.iface
        self.iface = want if want != "auto" and want in counters else auto_iface(list(counters), stats, counters)
        if self.iface != prev_iface:
            logging.info("monitor: interface -> %s (configured %s)", self.iface, want)
        # A link that comes back may be a different Wi-Fi network: re-read the name now
        # instead of waiting out the cache, so the first bytes after a reconnect are not
        # billed to the wrong network.
        up = bool(self.iface and self.iface in stats and stats[self.iface].isup)
        self.link_changed = self.link_up is not None and up and not self.link_up
        self.link_up = up

        t0, self.t_last = self.t_last, now
        elapsed = now - t0 if t0 is not None else 0
        limit = max(COUNTER_JUMP, int(400_000_000 * elapsed))  # 400 MB/s of headroom per second of window
        delta = {}
        for nic, c in counters.items():
            fwd = self._nic_delta(nic, (c.bytes_recv, c.bytes_sent), self.base.get(nic), limit)
            if fwd is not None:
                delta[nic] = fwd
            self.base[nic] = (c.bytes_recv, c.bytes_sent)
        if t0 is None or now <= t0 or self.iface not in delta:
            return None
        drx, dtx = delta[self.iface]
        return t0, drx, dtx

    def _record(self, t0, now, drx, dtx):
        """Credit a sample to the calendar day(s) its window really covered.

        Traffic kept only the day it was sampled on, so a window crossing midnight pushed
        yesterday's bytes into today (and into today's cap/alerts). Split it by time."""
        if now <= t0:
            self.store.add(date.fromtimestamp(now).isoformat(), drx, dtx)
            return
        first, last = date.fromtimestamp(t0), date.fromtimestamp(now)
        if first == last:
            self.store.add(last.isoformat(), drx, dtx)
            return
        span = now - t0
        day, sent_rx, sent_tx = first, 0, 0
        while day <= last:
            boundary = min(now, datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp())
            f = max(0.0, min(1.0, (boundary - t0) / span))
            up_rx, up_tx = int(drx * f), int(dtx * f)  # cumulative share through this day
            take_rx, take_tx = up_rx - sent_rx, up_tx - sent_tx
            if take_rx or take_tx:
                self.store.add(day.isoformat(), take_rx, take_tx)
            sent_rx, sent_tx = up_rx, up_tx
            day += timedelta(days=1)
        logging.debug("sample: split %d/%d bytes %s -> %s across midnight", drx, dtx, first, last)

    def _nic_delta(self, nic, cur, prev, limit):
        """One NIC's plausible delta, or None when there is nothing (or a lying counter) to count.

        The two directions are judged separately: an adapter reset often clears rx only (or
        tx only), and dropping the healthy direction with it silently under-counted every
        other sample after the reset. A value that cannot be traffic in this window is still
        rejected direction by direction, never credited."""
        if prev is None:
            return None
        out = []
        plausible = rejected = False
        for c, p in zip(cur, prev):
            d = c - p
            if d >= 0:
                if d > limit:
                    logging.warning("Implausible counter jump on %s ignored direction "
                                    "(prev=%s cur=%s)", nic, p, c)
                    out.append(0)
                    rejected = True
                else:
                    out.append(d)
                    plausible = plausible or d > 0
            elif c * 4 < p:  # backwards AND collapsed: a reset, not a corrected re-read
                out.append(c)  # count from what the counter has re-accumulated
                plausible = True
            else:
                # back near an old value (a doubled reading corrected) or too big for this
                # window: neither is traffic we can trust
                logging.warning("Implausible counter re-read on %s ignored direction "
                                "(prev=%s cur=%s)", nic, p, c)
                out.append(0)
                rejected = True
        if plausible or not rejected:  # idle readings still tick the ring; nothing else does
            return tuple(out)
        return None

    def _counted(self, cfg, now, drx, dtx, force=False):
        """True when this sample counts (configured hotspot in use; the toggle may veto it).

        `force` re-reads the Wi-Fi name even inside the cache window: a freshly opened
        dashboard is exactly when the user expects the network state to be current."""
        counted = True
        if cfg["ssid"]:
            big = drx + dtx >= 5_000_000
            if force or now - self.ssid_t >= self.SSID_EVERY or (big and now - self.ssid_t >= 3):
                prev_ssid = self.ssid
                self.ssid, self.ssid_t = get_ssid(), now
                logging.debug("monitor: Wi-Fi refresh -> %r (was %r, configured %r)",
                              self.ssid, prev_ssid, cfg["ssid"])
                if self.ssid != prev_ssid:
                    logging.info("monitor: Wi-Fi name changed %r -> %r (configured %r, on_network=%s)",
                                 prev_ssid, self.ssid, cfg["ssid"], self.on_network())
            # a hotspot is configured: only that network counts; None (name unreadable on
            # Wi-Fi) counts as "trust the adapter", the dashboard shows the state live
            counted = self.on_network() is not False
        if cfg.get("count_mode", "auto") == "off":  # the dashboard's toggle: count nothing at all
            counted = False
        return counted

    def _log_counting(self, cfg, counted):
        if self.counting != counted:
            logging.info("monitor: counting %s (ssid=%s configured=%s mode=%s)",
                         "ON" if counted else "OFF", self.ssid, cfg["ssid"],
                         cfg.get("count_mode", "auto"))
        self.counting = counted

    def nic_total(self):
        """Cumulative rx+tx bytes of the counted NIC, or None before the first reading.
        Callers diff two readings to get bytes moved over THEIR OWN window (the Apps
        tracker needs its 10 s window, not this sampler's 1-5 s one)."""
        base = self.base.get(self.iface) if self.iface else None
        return (base[0] + base[1]) if base else None

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

        def g(b):
            return f"{b / st.gb:.1f} GB"
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
        if not sys.platform.startswith("win"):
            return  # only Windows has this firewall
        st, cfg = self.store, self.store.cfg
        if not force and now - self.t_kill < 60:
            return
        self.t_kill = now
        from . import firewall  # local: only Windows has the firewall helper
        try:
            if self.kill_state is None:
                self.kill_state = firewall.cut_active()  # pick up a rule left over from a crash
            want = cfg["ssid"]
            mode_off = cfg.get("count_mode", "auto") == "off"  # counting off: hands off the firewall
            gate = self.on_network(want) if want else None
            off = gate is False
            over = st.summary(datetime.fromtimestamp(now))["today"] >= KILL_GB * st.gb
            auto_cut = st.fired.get("killday") == date.today().isoformat()  # the automatic cut, not a manual one
            if self._kill_want_restore(want, mode_off, off, over, auto_cut):
                self._kill_restore(firewall, off, over)
            elif self._kill_want_cut(gate, mode_off, over):
                self._kill_cut(firewall, now)
        except Exception as e:  # a firewall hiccup must never take the monitor down
            logging.warning("kill-switch: %s", e)

    def _kill_want_restore(self, want, mode_off, off, over, auto_cut):
        """True when a previously cut connection should be given back now."""
        if not self.kill_state:
            return False
        return (not want) or off or (not over) or (mode_off and auto_cut)

    def _kill_restore(self, firewall, off, over):
        ok, err = firewall.restore_internet()
        if ok:
            self.kill_state = False
            with self.store.lock:
                # one cut decision per day is tracked by killday; popping it on every successful
                # restore lets a return to the hotspot re-evaluate the cap
                self.store.fired.pop("killday", None)
                self.store.dirty = True
            logging.info("kill-switch: internet restored%s"
                         % (" (not over the cap anymore)" if not over else ""))
        else:
            logging.warning("kill-switch: restore failed: %s", err)

    def _kill_want_cut(self, gate, mode_off, over):
        """True when the hotspot is in use, over the cap, and today's cut hasn't fired yet."""
        if self.kill_state:
            return False
        on = gate is True and not mode_off
        return on and over and self.store.fired.get("killday") != date.today().isoformat()

    def _kill_cut(self, firewall, now):
        ok, err = firewall.cut_internet()
        if ok:
            self.kill_state = True
            with self.store.lock:
                self.store.fired["killday"] = date.today().isoformat()
                self.store.dirty = True
            st = self.store
            self.alert("kill", "Internet cut off - 10 GB daily cap reached",
                       f"{st.summary(datetime.fromtimestamp(now))['today'] / st.gb:.1f} GB used today "
                       "on the hotspot. The cap lifts tomorrow, or tap Restore in the dashboard.")
        else:
            if "declined" in (err or ""):  # refused elevation: retrying every minute is a UAC prompt loop
                with self.store.lock:
                    self.store.fired["killday"] = date.today().isoformat()
                    self.store.dirty = True
                    self.store.flush()
                logging.info("kill-switch: elevation declined - not asking again today")
            else:
                logging.warning("kill-switch: cut failed: %s", err)

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
        if on and st.cfg["ssid"] and self.on_network() is False:
            # the firewall is only ever touched on the configured hotspot: a cut made elsewhere
            # would just be auto-restored by check_kill within a minute
            return False, "not on the configured hotspot"
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
                logging.warning("monitor: %s", self.err)
            self.wake.wait(1.0 if time.time() - self.last_ui < 10 else 5.0)
            self.wake.clear()

    def ram_mb(self):
        """This process's own memory: the figure Task Manager shows for python.exe. The
        WebView2 children that draw the dashboard are separate processes, so they stay out."""
        return self.proc.memory_info().rss / 1048576

    def app_cpu_pct(self):
        """This process's CPU the way Task Manager reports it. psutil counts share of one
        core (a saturated core reads 100%), Task Manager counts share of the whole machine -
        on 12 cores the same moment reads 8.3% there. Divide to agree with it."""
        cores = psutil.cpu_count() or 1
        return self.proc.cpu_percent(interval=None) / cores

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
            warn_today = any(a.get("kind") in WARN_KINDS and str(a.get("t", "")).startswith(today.isoformat())
                             for a in self.store.log[-40:])
            s.update(cfg=dict(self.store.cfg), alerts=list(reversed(self.store.log[-10:])))
        # "about to be cut": the daily cap is reached and nothing has dealt with today yet
        pending = bool(s["cfg"].get("ssid")) and s["cfg"].get("count_mode", "auto") != "off" \
            and self.on_network() is not False \
            and s["today"] >= KILL_GB * self.store.gb \
            and not fired_kill and not bool(self.kill_state)
        s.update(iface=self.iface, ssid=self.ssid, counting=self.counting, err=self.err,
                 cpu=psutil.cpu_percent(interval=None), mem=psutil.virtual_memory().percent,
                 app_cpu=self.app_cpu_pct(),
                 ram_mb=self.ram_mb(),
                 warn_today=warn_today,
                 kill={"active": bool(self.kill_state), "pending": pending, "gb": KILL_GB, "fired": fired_kill},
                 ifaces=sorted(psutil.net_io_counters(pernic=True)),
                 down=rx[0] / 4, up=tx[0] / 4,
                 series_down=[b / 5 for b in srx], series_up=[b / 5 for b in stx], history=hist)
        return s
