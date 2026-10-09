- [x] blocked apps, should be only blocked if it's connected to the concerned wi-fi
- [] add bandwith limit for each app
  - dropped: Windows can only throttle upload per app (QoS policies are outbound-only);
    per-app download throttling requires a kernel driver (WinDivert/WFP callout, like
    NetLimiter) - out of scope for DataGuard
- [x] show download, upload for each app
  - Windows reports no per-process direction (psutil counters are cumulative only; the
    per-connection APIs are dead ends), so each app's bytes are split by the counted
    NIC's real rx:tx ratio for the same window (today vs cycle each use their own ratio)
- [x] hard block avast from using the internet (it still uses internet even tho it's blocked from the app)