from __future__ import annotations

from typing import Any


def _pixels(profile: dict[str, Any]) -> int:
    return int(profile.get("width") or 0) * int(profile.get("height") or 0)


def _quality(profile: dict[str, Any]) -> tuple[int, int, float]:
    return (_pixels(profile), int(profile.get("bitrate_kbps") or 0), float(profile.get("fps") or 0))


def _aspect_ratio(profile: dict[str, Any]) -> float:
    height = int(profile.get("height") or 0)
    return int(profile.get("width") or 0) / height if height else 0


def select_stream_roles(profiles: list[dict[str, Any]]) -> dict[str, str]:
    usable = [profile for profile in profiles if profile.get("uri") and profile.get("token")]
    if not usable:
        return {}
    recording = max(usable, key=_quality)
    recording_ratio = _aspect_ratio(recording)
    detection_candidates = [
        profile
        for profile in usable
        if str(profile.get("encoding") or "").upper() in {"H264", "H.264", "AVC"}
        and _pixels(profile) >= 320 * 180
        and (
            not recording_ratio
            or not _aspect_ratio(profile)
            or abs(_aspect_ratio(profile) - recording_ratio) / recording_ratio <= 0.1
        )
    ]
    detection = min(detection_candidates or usable, key=_quality)
    return {
        "record": str(recording["token"]),
        "detect": str(detection["token"]),
    }

