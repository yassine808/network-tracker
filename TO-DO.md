- [x] blocked apps, should be only blocked if it's connected to the concerned wi-fi
- [x] add bandwidth limit for each app
  - REMOVED (1.3.0): the suspension-based limiter never worked reliably (suspend
    did not stop traffic; testing was confounded by recycled PIDs and dead
    endpoints). The feature is gone from the code, UI and API; a blocked app is
    the supported way to keep an app offline.
- [x] show download, upload for each app
  - not possible per-process on Windows (counters are cumulative, no direction);
    each app's bytes are reconciled to the Overview's daily totals instead
- [x] hard block avast from using the internet (it still uses internet even tho it's blocked from the app)
- [x] blocked apps vanished from the list after a month reset
  - the server now always lists every remembered blocked app (zeroed when it
    has no usage rows this cycle) and sends the blocked set to the dashboard
- [x] blocked apps (e.g. NVIDIA Broadcast) still recorded internet usage
  - a blocked app's deltas are skipped while its block is enforced, and blocking
    one exe now also covers every sibling exe in its install folder
