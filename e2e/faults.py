from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request


GO2RTC_URL = "http://127.0.0.1:1984"


def streams() -> dict[str, object]:
    with urllib.request.urlopen(f"{GO2RTC_URL}/api/streams", timeout=3) as response:
        state = json.load(response)
    if not isinstance(state, dict):
        raise RuntimeError("go2rtc returned invalid stream state")
    return state


def delete_managed_stream() -> None:
    managed = sorted(name for name in streams() if str(name).startswith("stream_"))
    if not managed:
        raise RuntimeError("No managed stream is available for drift injection")
    stream_key = str(managed[0])
    query = urllib.parse.urlencode({"src": stream_key})
    request = urllib.request.Request(
        f"{GO2RTC_URL}/api/streams?{query}",
        method="DELETE",
    )
    with urllib.request.urlopen(request, timeout=3):
        pass
    if stream_key in streams():
        raise RuntimeError("Managed stream deletion did not take effect")
    print(f"deleted managed stream: {stream_key}")


def main() -> int:
    if sys.argv[1:] != ["delete-managed-stream"]:
        print("usage: faults.py delete-managed-stream", file=sys.stderr)
        return 2
    delete_managed_stream()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
