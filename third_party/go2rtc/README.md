# Packaged go2rtc source patch

The Docker build downloads upstream commit
`b5948cfb25404cc5cb37b166ecaa2dca20b11d4b` (v1.9.14), verifies the archive
SHA-256, and applies `bounded-setup.patch` without fuzz. The pinned native Go
builder runs race-enabled concurrency tests before producing a static binary
for the requested runtime architecture. The original license is retained.

The patch limits simultaneous consumer setup to 16 per stream, including
cleanup, while leaving established readers unrestricted. Failed initial dials
share a one-second cooldown. Queued abandoned callers therefore reuse a failed
attempt instead of each performing another slow dial; further callers cannot
accumulate without bound. The failure cache is keyed to the source, so a changed address bypasses it. Normal
RTSP network deadlines and existing connected-reader reconnection remain intact.
The first admitted setup can run until its existing network deadline; this is
bounded admission and shared failure, not disconnect-aware network cancellation.

`retention_test.go` exercises concurrency, cleanup accounting, cooldown expiry
and source changes. `e2e/rtsp_retention.py` verifies the shipped binary against
real short-lived RTSP clients, unavailable network addresses, healthy video and
recovery video. Existing snapshot-memory and full integration gates still run.
