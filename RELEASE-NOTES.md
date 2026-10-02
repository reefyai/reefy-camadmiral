# CamAdmiral v2026.10.01-00

- Silence alerts for a single camera from its actions menu ("Silence alerts" /
  "Unsilence alerts"). Use it for a camera that flaps between offline and recovered
  while its hardware or cabling is being fixed. Silenced cameras show "Alerts
  silenced" in the camera list.
- Silenced cameras keep recording incidents, so availability history and the
  incident list stay complete. Alerts already queued when a camera is silenced are
  discarded.
- A "recovered" alert is sent only when its matching "offline" alert was sent, so
  unsilencing a camera during an outage never produces an unexpected recovery.
- Preserve existing camera identities, RTSP URLs, credentials and Frigate bindings.

Validation covers silenced, unsilenced and in-progress outages through the incident
pipeline, the new camera notifications endpoint, and the full release gate.
