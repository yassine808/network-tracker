# Progress — DataGuard (network-tracker)

## Plan
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
