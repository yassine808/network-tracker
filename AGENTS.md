# AGENTS.md — DataGuard (network-tracker)

Windows-first desktop data meter: counts every byte this laptop uses toward a metered
hotspot plan, warns before the cap, can cut the internet. Local only, no cloud.

Read `README.md` (the real manual), `PRODUCT.md` (who/why/voice), `DESIGN.md` (tokens),
`TO-DO.md` (known open items) before changing behaviour.

## Shape

- `dataguard.py` → `dataguard.cli.main()`. Everything else lives in `dataguard/`.
- One database, one source of truth: `%APPDATA%\DataGuard\dataguard.db`.
- `settings.py` owns defaults/validation/cycle math; `usage.py` the Store; `live.py` the
  sampling loop + alert/kill-switch state machine; `apps.py` per-app bytes; `web.py` the
  localhost HTTP/JSON API; `dashboard.html` is self-contained (inline CSS/JS, no CDN).
- Version lives in `dataguard/__init__.py` and must equal the release tag.

## Rules that are easy to break

- **One hard dependency: `psutil`.** `pillow`, `pystray`, `pywebview` are optional and must
  degrade gracefully (dashboard still works in a browser).
- **The app must not use the internet.** Only `127.0.0.1` is allowed (dashboard + self-seal
  firewall rules). No update checks, no telemetry, no remote assets in the dashboard.
- **Python floor is 3.8** (runtime 3.9.13). Do not use 3.10+ syntax. `pillow` is pinned to
  11.3.0 *because* of that floor — see `TO-DO.md` before touching it.
- **Counting is gated on the configured SSID.** Off-network means record nothing and alert
  nothing. Never bill a stale network name to the plan.
- Fail loudly: no silent `except: pass` around anything that matters.
- Status colour is always paired with a word; motion only on state change (see `DESIGN.md`).

## Verify before claiming done

```powershell
python tests/smoke.py        # exit 0 = release gate (39 checks, ends with node --check)
ruff check .                 # rules pinned in ruff.toml
python -m compileall dataguard dataguard.py
```

All three run in CI (`.github/workflows/ci.yml`) on `windows-latest` alongside bandit and
pip-audit. A `v*` tag builds the zip + `setup.exe` via `installer/build.ps1`.

## Working here

- Stay inside the project root; the DB and logs at `%APPDATA%\DataGuard\` are read-only
  unless the task is explicitly about data repair.
- **Restart the app after every change to `dashboard.html`.** `frontend.py` reads the page
  into `PAGE` at import, so a running window keeps serving the copy it loaded at startup.
  Stop the listener on 8787, relaunch `python dataguard.py run --open`, and confirm the
  served page carries a marker unique to the change.
- Do not `git add .`, commit, tag, or push unless asked.
- `build/`, `dist/`, `__pycache__/`, `.ruff_cache/` are generated — never hand-edit.
