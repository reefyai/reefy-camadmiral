# Patched go2rtc

CamAdmiral builds go2rtc from the upstream `v1.9.14` source at commit
`b5948cfb25404cc5cb37b166ecaa2dca20b11d4b`. The immutable commit archive is
verified with SHA-256
`78aa79bcedec8f155e4060a379613979b0b3ee48ff62ee5164bafc0ac6532386`.

`patches/0001-live-source-handover.patch` adds the behavior CamAdmiral needs when a
camera keeps its ONVIF identity but moves to a new address:

- `PATCH /api/streams` prepares and starts the new producer without replacing
  the existing stream or downstream consumer.
- Active receiver tracks move as one transaction before the old producer is
  stopped.
- Packets are ordered across that move, and existing RTSP consumers retain a
  continuous SSRC, RTP sequence, and timestamp timeline.

The patch addresses the upstream behavior described in
[go2rtc issue #2404](https://github.com/AlexxIT/go2rtc/issues/2404). The Docker
build applies the patch, formats the touched Go files, runs the stream/core
packages and focused RTSP handoff tests with the race detector, and then builds
the target architecture.

When updating go2rtc, update the pinned commit and archive checksum in the
Dockerfile, rebase the patch on that exact source, and run the complete Docker
E2E gate.

go2rtc is distributed under the MIT License. A copy is in `LICENSE`.
