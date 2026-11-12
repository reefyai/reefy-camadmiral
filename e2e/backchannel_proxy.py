"""Synthetic camera: ordinary A/V works, advertised talkback SETUP is malformed."""
import asyncio
import json
import os
import re
from urllib.parse import urlsplit, urlunsplit

stats = {"describes": 0, "backchannel_requests": 0, "broken_setups": 0,
         "plays": 0, "active": 0, "max_active": 0, "dropped_packets": 0, "dropped_audio_packets": 0}
stalled = False
empty_audio = os.environ.get("EMPTY_AUDIO_TRACK") == "1"


async def message(reader):
    first = await reader.readexactly(1)
    if first == b"$":
        header = await reader.readexactly(3)
        return first + header + await reader.readexactly(int.from_bytes(header[1:], "big"))
    header = first + await reader.readuntil(b"\r\n\r\n")
    match = re.search(rb"(?im)^Content-Length:\s*(\d+)", header)
    return header + await reader.readexactly(int(match.group(1)) if match else 0)


async def control(reader, writer):
    global stalled, empty_audio
    path = (await reader.readline()).split()[1]
    if path == b"/stall":
        stalled = True
    elif path == b"/resume":
        stalled = False
    elif path == b"/audio/stall":
        empty_audio = True
    elif path == b"/audio/resume":
        empty_audio = False
    body = json.dumps(dict(stats, stalled=stalled, empty_audio=empty_audio)).encode()
    writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                 + str(len(body)).encode() + b"\r\n\r\n" + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def advertise_broken_backchannel(data):
    header, body = data.split(b"\r\n\r\n", 1)
    parts = re.split(rb"(?m)(?=^m=)", body)
    for i, part in enumerate(parts):
        if part.startswith(b"m=audio"):
            part = re.sub(rb"(?m)^a=(?:sendonly|recvonly|sendrecv)\r?\n", b"", part)
            parts[i] = part.rstrip(b"\r\n") + b"\r\na=sendonly\r\n"
    body = b"".join(parts).rstrip(b"\r\n") + (
        b"\r\nm=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        b"a=recvonly\r\na=control:trackID=2\r\n")
    header = re.sub(rb"(?im)^Content-Length:[^\r\n]+",
                    b"Content-Length: " + str(len(body)).encode(), header)
    return header + b"\r\n\r\n" + body


def is_microphone_packet(data):
    # The synthetic microphone is PCMU (RTP payload type 0). Track/channel
    # order can vary, so a channel number must not decide which media to mute.
    return (len(data) >= 16 and data[0] == 36 and data[4] >> 6 == 2
            and data[5] & 0x7f == 0)


async def relay(reader, writer):
    upstream = None
    task = None
    pending = asyncio.Queue()
    counted = False
    try:
        source, upstream = await asyncio.open_connection("camera-open", 8554)

        async def requests():
            nonlocal counted
            while True:
                data = await message(reader)
                if data.startswith(b"$"):
                    upstream.write(data)
                    await upstream.drain()
                    continue
                first, rest = data.split(b"\r\n", 1)
                method, uri, version = first.split(b" ", 2)
                parsed = urlsplit(uri.decode())
                backchannel = b"ver20/backchannel" in data
                if method == b"DESCRIBE":
                    stats["describes"] += 1
                    stats["backchannel_requests"] += int(backchannel)
                if method == b"SETUP" and parsed.path.endswith("trackID=2"):
                    stats["broken_setups"] += 1
                    cseq = re.search(rb"(?im)^CSeq:\s*(\d+)", data).group(1)
                    # No Transport or terminating empty line: client waits, then retries.
                    writer.write(b"RTSP/1.0 200 OK\r\nCSeq: " + cseq +
                                 b"\r\nSession: synthetic;timeout=120\r\n")
                    await writer.drain()
                    continue
                if method == b"PLAY" and not counted:
                    counted = True
                    stats["plays"] += 1
                    stats["active"] += 1
                    stats["max_active"] = max(stats["max_active"], stats["active"])
                path = re.sub(r"^/(?:healthy|faulty)", "/main", parsed.path)
                rewritten = urlunsplit(parsed._replace(netloc="camera-open:8554", path=path))
                await pending.put((method, backchannel, parsed.path.startswith("/faulty")))
                upstream.write(method + b" " + rewritten.encode() + b" " + version + b"\r\n" + rest)
                await upstream.drain()

        task = asyncio.create_task(requests())
        task.add_done_callback(lambda _: upstream.close())
        while True:
            data = await message(source)
            if data.startswith(b"$"):
                muted_microphone = empty_audio and is_microphone_packet(data)
                if stalled or muted_microphone:
                    stats["dropped_packets"] += 1
                    stats["dropped_audio_packets"] += int(muted_microphone)
                    continue
            else:
                method, backchannel, faulty = await pending.get()
                if method == b"DESCRIBE" and backchannel and faulty and data.startswith(b"RTSP/1.0 200"):
                    data = advertise_broken_backchannel(data)
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        if counted:
            stats["active"] -= 1
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if upstream:
            upstream.close()
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def main():
    rtsp = await asyncio.start_server(relay, "0.0.0.0", 8554)
    api = await asyncio.start_server(control, "0.0.0.0", 8080)
    async with rtsp, api:
        await asyncio.gather(rtsp.serve_forever(), api.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
