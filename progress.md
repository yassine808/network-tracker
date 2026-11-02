# Progress — DataGuard (network-tracker)

## Plan
- [x] Find why the Apps rows and the Overview disagree, and close the gap for good
- [x] Make short-lived processes land on their own real name, not "pid N"
- [x] Ship 1.4.0: bump, pass all four gates locally, tag, publish the exe
- [x] Make the Overview counter lose no bytes: bridge app downtime, keep the midnight
      remainder, fix `auto` picking a virtual or dead adapter
- [x] Ship 1.4.1: bump, gates, build `DataGuard-Setup-v1.4.1.exe`, tag, publish
- [ ] (nothing in flight — awaiting next task)

## Log

### 2026-11-01 — session start, project orientation
- Goal: open the project and establish ground truth before any change.
- Done: read README/TO-DO/PRODUCT/requirements/CI; confirmed tree, version 1.3.2 at tag
  v1.3.2, clean working tree; wrote `AGENTS.md` (project rules were missing).
- Verified: `git status --short` empty; `git describe` → `v1.3.2`; `dataguard/__init__.py`
  `__version__ = "1.3.2"`; Python 3.9.13, Node v24.22.1 present. Tests not run yet.
- Next: awaiting the actual task.

### 2026-11-01 — dashboard redesign committed and pushed
- Goal: get the worktree onto the remote.
- Done: a 478+/231- redesign of `dataguard/dashboard.html` appeared in the worktree
  (brand mark, icon nav + sliding indicator, hero status strip, dismissible guide, panel
  restyle, reworked canvas chart). Committed as `b64ae81`, plus `AGENTS.md` and this log as
  `eca7e9c`; pushed `6d26f7b..eca7e9c` to `origin/main`.
- Verified: `python tests/smoke.py` exit 0 (includes `node --check` of the new inline
  script); `git status -sb` clean and level with `origin/main`.
- Open finding: `dashboard.html:7-8` loads Google Fonts over HTTPS. That predates this
  change and contradicts the "no internet, localhost only" rule — the self-seal firewall
  blocks it anyway, so the page falls back to system fonts.
- Next: the frontend split request is still unresolved (two readings: move the page out of
  `dataguard/`, or extract its CSS/JS). GitHub reports 58 Dependabot alerts on `main`
  (5 critical), all tied to the pillow / Python-floor item in `TO-DO.md`.

### 2026-11-01 — running as a desktop window
- Goal: run the app as a native window instead of a browser tab.
- Done: installed the pinned extras into Python 3.9.13 — pillow 11.3.0, pystray 0.19.5,
  pywebview 6.2.1 (pulls pythonnet 3.0.5, bottle, clr_loader, cffi); restarted the monitor
  so `shell.py` takes the `HAVE_SHELL` path instead of `_fallback`.
- Verified: python PID 22080 owns a visible, non-minimized 1100x740 window titled
  "DataGuard"; WebView2 runtime 155.0.4283.62 is installed; `/` still answers 200 on
  127.0.0.1:8787; the log has no "App window unavailable" fallback line.
- Next: unchanged — the frontend split is still undecided. This entry is not committed.

### 2026-11-01 — second dashboard pass pushed
- Goal: get the rail/tick-dial rework onto the remote.
- Done: `dashboard.html` changed again under us (383+/365-): top nav → left rail with
  keyboard hints, cycle ring → 72-tick dial, today's bar → 48-segment meter, 30-day
  heatmap → bars with a dashed limit line, settings reflowed into label/field sections
  with a "Use current Wi-Fi" button. Committed as `03b026d`.
- Verified: `python tests/smoke.py` exit 0 (`node --check` passes on the new inline
  script); the only remote URL is still the pre-existing `fonts.googleapis.com` link
  (Manrope range widened to 300..800 in this pass).
- Next: the running window (PID 17572) still serves the page it imported at 04:17:56, so
  it shows the previous design until the app is relaunched.

### 2026-11-01 — third dashboard pass pushed
- Goal: get the top-bar / single-screen overview redesign onto the remote.
- Done: rail removed again, top bar restored; overview became a single-screen grid (cycle
  ring left, today + live right); live chart rebuilt on a 60-sample sliding window with
  separate download/upload panes. Committed as `aa9338d` and pushed.
- Verified: `tests/smoke.py` exit 0; after relaunch `/` served the new markers
  (`id="top"`, `class="ov"`, `id="ringbox"`, no `id="rail"`).
- Next: the overview still scrolled ~45px at 1100x740 because `[data-p="main"]` carried
  `min-height:600px` on top of the hero and gaps.

### 2026-11-01 — overview fits one screen; 30-day bars stop re-rendering
- Goal: "show everything without scrolling, compact", then fix "last 30 days keeps
  re-showing every second".
- Done: dropped the 600px floor, tightened the vertical rhythm (top bar 62→56, main
  padding 16→14, hero text 22/13.5→20/12.5, panel padding 14/18 for overview panels, ring
  clamp 128–236px, `.big` 44→36, `#tbar` 3vh, `#hm` 5vh). Rewrote `heat()`: the DOM is
  built once per date list and later polls only patch bar height/class, so the `grow`
  animation no longer restarts every second; the chart peak now tracks usage instead of
  the daily allowance, so a big plan no longer flattens every bar.
- Verified: window capture at 1100x740 shows the whole overview, no scrollbar; two
  captures 3 s apart are pixel-identical in the 30-day panel (0 diff pixels); today's bar
  renders full height (1016 blue pixels). `tests/smoke.py` exit 0.
- Next: `dataguard/dashboard.html` is modified but uncommitted. The plan is currently
  set to 999 GB (set during testing) — restore it to the real plan if that was a slip.
### 2026-11-01 — nav centered; README screenshots refreshed
- Goal: “put overview, apps and settings in the middle” of the top bar, then “update
  readme.md with new screenshots”.
- Done: `#top` is now `display:grid;grid-template-columns:1fr auto 1fr`, so the nav sits in
  the middle regardless of how wide the brand and the right cluster get; the <=900px rule
  restores `display:flex` before it wraps. Re-shot `docs/screenshots/overview.png`,
  `apps.png` and `settings.png` at 1084x701 and corrected the Settings caption to the cards
  the shot actually shows.
- Verified: `/` serves the grid rule; the captures match the app window pixel-for-pixel (same
  fonts, same layout, nav centred); `tests/smoke.py` exit 0; `ruff check dataguard/ dataguard.py`
  clean. `ruff check .` still flags E402 in `tests/smoke.py` — pre-existing, and CI lints only
  the package.
- Next: uncommitted — `README.md`, `dataguard/dashboard.html` (nav centering) and the three
  PNGs. The app was left running without a window (tray icon) so the shots could be taken
  without touching the desktop.

### 2026-11-01 — Apps and Overview now add up to the byte
- Goal: "find out why there is a difference, and keep fixing bugs etc! make the app 100%
  accurate" — the Overview total, the sum of the Apps rows, and each app's own row.
- Why they differed: measured first, then fixed. Two causes.
  1. A fixed `scale = 1.04` plus a once-a-minute `_reconcile` that compared two counters
     sampled at different instants. Every minute it added whatever was "missing" to the
     System row, then the late ETW events for those same bytes arrived and were counted
     again: System grew to 45-62 MB and the Apps sum swung ±13 MB around the Overview
     (`reconcile: ... -> +12960470 on System` right in the middle of a 20 MB download).
  2. Recycled pids inherited dead processes' names. A 20 MB `curl` download landed on
     `WmiPrvSE.exe` (10,428,273 B) because the pid had belonged to WmiPrvSE before curl got
     it; short-lived programs whose events arrived after they exited were filed as `pid N`.
     Evidence: `tests/probe.json` (a curl run credited 100% to `usque.exe`), and a direct
     20 MB test that booked 10.4 MB twice, once to `curl.exe`, once to `WmiPrvSE.exe`.
- Done:
  - `apps._apportion`: every ten-second tick now splits the bytes the **Overview itself**
    just added to today over the apps in proportion to the payload ETW measured for them —
    integer-exact, remainder to the biggest mover. The rows therefore cannot drift from the
    meter or exceed it: they follow it, at most one tick behind. Wire bytes no app claims go
    to System, never smeared over apps. The fixed 4% and the accumulation of System both
    went away.
  - `apps._wire_delta`: the tick's byte figure is the increment of the Overview's own daily
    total. A first version read the NIC counter separately for the tracker's window; it
    tracked closely (±13 KB on a 14 MB burst) but needed an interface-change guard and a
    jump guard, and could bill windows the sampler had refused to credit. Reading the meter's
    own figure makes "apps sum == Overview" structural instead of approximately true, so the
    second counter read and both guards were deleted.
  - `apps._reconcile`: now settles **finished** days only. Comparing a live day against a
    sampler up to one window behind is what kept System growing and shrinking by megabytes a
    minute; once the day stops moving the comparison is meaningful, and it repairs what the
    ticks could not see (the seconds before a start, a killed run's lost rows).
  - `apps._app_for_pid` + caches: pid entries now carry the process creation time, and the
    newest owner wins, so a recycled pid can no longer inherit a dead app's name.
  - `netwatch`: same ETW session also enables `Microsoft-Windows-Kernel-Process`
    (keyword 0x10) and remembers each process's image name at ProcessStart, so a downloader
    that lives 300 ms still gets its own row. The payload template has changed three times
    (this build sends version 4) and TDH would not decode it, so the parser relies only on
    what is fixed: the pid leads the payload and the image path is the UTF-16 string ending
    in `.exe`.
  - `apps._tick_estimate` (non-admin fallback): apportioned the same way, no more `scale`
    capped at 1.0.
  - `live.Monitor.nic_total` removed (nothing calls it any more).
  - README "Measured, not guessed" / "Reconciled to the Overview" rewritten to match.
  - `tests/smoke.py`: `test_app_allocation` (11 checks) — apportionment sums and rounding,
    an idle meter, a tick with no payload, the pid-reuse/name rules, and the ProcessStart
    payload parser.
  - Diagnostics kept: `tests/gap.py` (Overview vs Apps vs DB, right now) and
    `tests/watch.py` (one known download, sampled every 5 s).
- Verified: `python tests/smoke.py` exit 0 (70 checks, incl. the ProcessStart parser), `ruff
  check dataguard/ dataguard.py` clean, `compileall` clean. Live, with the real app on the
  real adapter:
  - 5 x 2 MB direct curl, last build: `curl.exe +10,462,694` for 10,000,000 bytes of payload
    (the 4.6 % is TCP/HTTP overhead, now inside the app's number), `96 start(s)` decoded, no
    `pid N` row, and the diag did not move by a single byte during the transfer.
  - 2 x 10 MB direct curl: `+10,617,336` then `+10,583,332`, `System +0` both times. Under
    the intermediate build the Apps sum tracked the Overview to 13 KB on a 14 MB burst; the
    final build is exact by construction, so it is one tick (10 s of traffic, a few MB)
    behind instead of a few KB out.
  - `tests/watch.py`, full run: 20 MB moved, `System +0` throughout, diag drift constant.
- Note: today's own rows still carry ~13.9 MB of System inflation created by the old
  reconcile while it was being tested (`System cycle=25,206,274`). Today is now left alone,
  so it stands for the rest of the day; at midnight the settled-day reconcile takes exactly
  that difference back off System (it has 25 MB to give) and the day ends exact.
- Next: nothing in flight. Proxied traffic (system HTTP proxy on a local virtual adapter) is
  still billed to the process that really sends it — the client's own socket never touches a
  wire; pairing a local proxy's upstream connections to its clients is not something ETW
  exposes.

### 2026-11-01 — 1.4.0 released
- Goal: cut a new installer (`DataGuard-Setup-v<version>.exe`) and publish it to GitHub
  Releases.
- Done: bumped `dataguard/__init__.py` to 1.4.0 and the three README references (download
  link, current-version line, release-pipeline diagram) in `d88611c`; committed the accuracy
  probe (`tests/probe.py`) and its recorded run (`tests/probe.json`) alongside, at Yassine's
  request. Tagged `v1.4.0`, pushed `main` + tag.
- Failure and fix: the first Release run (`41302599917`) failed in the bandit job — four
  `B110 try_except_pass` findings at `apps.py:347`, `apps.py:1009`, `netwatch.py:403`,
  `netwatch.py:409`, all from `e656ae3`. That CI failure predates this session (the 11:31 run
  on `main` failed the same way) and was invisible locally because **bandit is not installed
  on this machine** — only the three AGENTS.md gates were being run. The four sites now log
  at debug with the traceback instead of swallowing the error (`a736e3b`), which is also what
  "fail loudly" asks for. Tag moved to that commit (deleted remote tag, re-pushed; no release
  had been published yet).
- Verified: locally — `python tests/smoke.py` exit 0 (70 checks), `ruff check dataguard/
  dataguard.py` clean, `compileall` clean, and `bandit -r dataguard -q -s
  B104,B310,B404,B603,B607` exit 0 with bandit 1.8.6 installed (the CI pin) after reproducing
  the same four findings first. On GitHub — Release run `41302891356` success (5 checks, tag
  == `__version__`, zip + setup.exe built), CI run `41302881328` on `main` success, release
  `v1.4.0` published with `DataGuard-Setup-v1.4.0.exe` (13.17 MB) and `dataguard-v1.4.0.zip`.
- Note: the debug lines land below the default log level, so a normal run records nothing new.
- Next: install `bandit==1.8.6` (and ideally pip-audit) locally, or the local gate will keep
  disagreeing with CI. The pillow / Python-floor item in `TO-DO.md` still accounts for the 58
  Dependabot alerts GitHub reports on every push.

### 2026-11-02 — the meter loses no bytes across downtime, midnight and a bad interface pick
- Goal: "make the counting as accurate as possible", scoped with Yassine to the **Overview
  total** (not per-app, not carrier calibration). The total already matched the counted NIC
  counter; the task was every path where bytes could still be dropped.
- Done:
  - `usage.Store`: new `anchor` table holding the NIC counters, boot time, interface, SSID and
    the gate at the last reading. `flush()` writes it in the **same transaction** as the day
    rows (an anchor newer than the rows would double-count; older would lose).
  - `live.Monitor._save_anchor` runs after every reading; `_recover` runs once on the first
    reading after a start and credits the counter delta since the anchor. Refused unless the
    boot is the same, the counted interface is the same, counting was on when it was saved, the
    metered network is in use now, the counters only moved forward, and the gap is
    ≤ `RECOVER_MAX` (3600 s). A mismatch is logged; nothing is guessed.
  - The first reading now evaluates the gate too, so the very first anchor carries it.
  - `_record`: the last day of a midnight split takes the **remainder** instead of a truncated
    fraction (a float at 0.9999999 used to shave a byte).
  - `interfaces`: `VIRTUAL` gained `virtual` (catches "Microsoft Wi-Fi Direct Virtual Adapter"),
    `wi-?fi direct`, `hosted network`, `wan miniport`, `wireguard`, `openvpn`, `vmnet`.
    `auto_iface` now ignores virtual adapters, prefers an **up** real Wi-Fi radio (ties by bytes
    carried, so it cannot flicker), and when no Wi-Fi is up counts the busiest real adapter that
    *is* up instead of a dead radio.
  - `AppTracker._wire_delta`: the first tick after a start hands out `Overview.today − app rows`,
    so the recovered downtime (and the seconds before the first tick) still appear in the Apps
    sum instead of leaving the panel behind the meter until midnight.
  - README: "Downtime" and "Interface pick" bullets, the sampling diagram, and `RECOVER_MAX` in
    the hard-coded values (also corrected `LOG_CAP`, documented as 3 MB, to its real 16 MB).
  - `tests/smoke.py`: `test_recover_gap` (11 checks: credit, no double credit, boot/interface/
    gate/network/backwards/too-long refusals, flush round-trip), `test_auto_iface` (3 checks),
    a midnight-remainder check, and a first-tick Apps check.
- Verified: `python tests/smoke.py` exit 0 (87 checks), `ruff check dataguard/ dataguard.py`
  and `ruff check tests/smoke.py --ignore E402` clean, `compileall` clean. Live on the real
  Ethernet adapter, temp home: session 1 credited 77,258/42,196 B and saved an anchor; 3 MB was
  downloaded with nothing running; the next start credited 3,986,036/1,213,770 B — the NIC
  delta since the anchor, to within the post-sample traffic (115 B) — and a second restart
  credited only the new traffic (1,271,129 B), so no byte was counted twice.
- Limits kept on purpose: a window straddling a Wi-Fi change is still dropped whole (crediting
  it would bill one network for the other's bytes), and a gap beyond one hour is left out (it
  cannot prove the laptop stayed on the hotspot).
- Next: nothing in flight. Remaining error sources are the transition window above, a gap
  longer than an hour, and an adapter whose counter resets.

### 2026-11-02 — ship 1.4.1 (the counting fix)
- Goal: put the byte-counting work on a tag and publish the setup exe.
- Done: bumped __version__ to 1.4.1 and the README version/download/filename/diagram references;
  installed Inno Setup 6.7.3 (winget); built `dist\DataGuard-Setup-v1.4.1.exe` locally with
  `installer\build.ps1 -Version v1.4.1` (payload smoke test passed, ISCC compile 8.7 s, 13.2 MB);
  committed by name, tagged `v1.4.1`, pushed `main` and the tag; the Release workflow built
  the zip + setup exe and published them.
- Verified: `python tests/smoke.py` exit 0 (87 checks), `ruff` and `compileall` clean before
  the build; payload reports 1.4.1; CI Release run 41493383277 completed successfully; the release
  has `DataGuard-Setup-v1.4.1.exe` (13,812,258 B) and `dataguard-v1.4.1.zip` (100,850 B).
- Next: nothing in flight.
