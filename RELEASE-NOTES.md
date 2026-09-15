# CamAdmiral v2026.09.14-02

- Allow up to 30 seconds to obtain a snapshot, accommodating slower stream startup.
- Monitor selected streams independently; a failed detect-stream snapshot no longer
  marks the record stream offline.
- Run background snapshots in a bounded worker pool without blocking active-stream
  monitoring. Local worker exhaustion is not treated as a camera failure.
- Retain bounded, killable FFmpeg snapshot processes from the previous update,
  preventing abandoned go2rtc keyframe consumers and memory growth on stalled sources.
- Preserve existing camera identities, RTSP URLs, credentials and Frigate bindings.

Validation covers delayed real RTSP frames, an isolated profile outage and recovery,
snapshot cancellation cleanup, production memory/PID limits, and the full release gate.
