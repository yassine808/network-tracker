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
