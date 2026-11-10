"""Synthetic RTSP fault: keep control replies working but discard media packets."""
import asyncio
import re
import json
import time
import os
from urllib.parse import urlsplit, parse_qs

stalled = False
dropped = 0
stalled_path = None
delay_until = 0.0
rejected_path = None
auth_requests = 0
active_streams = {}
describes = {}
session_limit = int(os.environ.get('RTSP_SESSION_LIMIT', '0'))
sessions = {}
session_denials = 0
fail_available = 0
stall_available = False
stalled_sessions = 0
video_connections = {}
frozen_video_packets = 0


async def control(reader, writer):
    global stalled, stalled_path, delay_until, rejected_path, fail_available, stall_available
    line = await reader.readline()
    path = line.split()[1]
    if path == b'/freeze-current-video':
        for connection in video_connections.values():
            connection['frozen'] = True
    elif path == b'/freeze-all-video':
        for connection in video_connections.values():
            connection['frozen'] = True
        os.environ['FREEZE_ALL_VIDEO'] = '1'
    elif path == b'/stall':
        stalled = True
    elif path == b'/resume':
        stalled = False
        os.environ.pop('FREEZE_ALL_VIDEO', None)
        for connection in video_connections.values():
            connection['frozen'] = False
        stalled_path = None
        delay_until = 0.0
        rejected_path = None
        fail_available = 0
        stall_available = False
    elif path == b'/fail-available':
        fail_available = 3
    elif path == b'/stall-available':
        stall_available = True
    elif path.startswith(b'/reject/'):
        rejected_path = path.removeprefix(b'/reject/').decode()
    elif path.startswith(b'/stall/'):
        stalled_path = path.removeprefix(b'/stall/').decode()
    elif path.startswith(b'/delay?'):
        seconds = float(parse_qs(urlsplit(path.decode()).query)['seconds'][0])
        delay_until = time.monotonic() + max(0, min(60, seconds))
    body = f'stalled={stalled} dropped={dropped}'.encode()
    if path == b'/auth-stats':
        body = json.dumps({'requests': auth_requests}).encode()
    if path == b'/stream-stats':
        body = json.dumps({'active': active_streams, 'describes': describes,
                           'sessions': sessions, 'session_limit': session_limit,
                           'session_denials': session_denials, 'stalled_sessions': stalled_sessions,
                           'fail_available': fail_available, 'frozen_video_packets': frozen_video_packets,
                           'video_sessions': len(video_connections)}).encode()
    writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: '
                 + str(len(body)).encode() + b'\r\n\r\n' + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def relay(reader, writer):
    global dropped, session_denials, fail_available, stall_available, stalled_sessions, frozen_video_packets
    connection = {'frozen': bool(os.environ.get('FREEZE_ALL_VIDEO'))}
    video_connections[id(writer)] = connection
    upstream = None
    task = None
    stream_name = ''
    counted_name = None
    slot_name = None
    hold_media = False
    try:
        source, upstream = await asyncio.open_connection('camera-open', 8554)

        async def requests():
            nonlocal stream_name, counted_name, slot_name, hold_media
            global auth_requests, session_denials, fail_available, stall_available, stalled_sessions
            while True:
                first = await reader.readexactly(1)
                if first == b'$':
                    header = await reader.readexactly(3)
                    data = first + header + await reader.readexactly(int.from_bytes(header[1:], 'big'))
                else:
                    data = first + await reader.readuntil(b'\r\n\r\n')
                    length = next((int(line.split(b':', 1)[1]) for line in data.split(b'\r\n')
                                   if line.lower().startswith(b'content-length:')), 0)
                    data += await reader.readexactly(length)
                match = re.search(rb'(?:DESCRIBE|PLAY) (rtsp://[^\s]+)', data)
                if match:
                    stream_name = urlsplit(match.group(1).decode()).path.strip('/')
                if data.startswith(b'DESCRIBE '):
                    describes[stream_name] = describes.get(stream_name, 0) + 1
                    if counted_name is None:
                        counted_name = stream_name
                        active_streams[counted_name] = active_streams.get(counted_name, 0) + 1
                if data.startswith(b'DESCRIBE ') and stream_name == rejected_path:
                    auth_requests += 1
                    cseq = re.search(rb'(?im)^CSeq:\s*(\d+)', data).group(1)
                    writer.write(b'RTSP/1.0 401 Unauthorized\r\nCSeq: ' + cseq +
                                 b'\r\nWWW-Authenticate: Digest realm="synthetic", nonce="test-nonce"\r\nContent-Length: 0\r\n\r\n')
                    await writer.drain()
                    continue
                if (session_limit and stall_available and data.startswith(b'DESCRIBE ')
                        and sessions.get(stream_name, 0) < session_limit):
                    # Freeze before SDP reaches the client, so interruption is
                    # unambiguously before adoption even with cached codec data.
                    slot_name = stream_name
                    sessions[slot_name] = sessions.get(slot_name, 0) + 1
                    stall_available = False
                    stalled_sessions += 1
                    await reader.read()
                    return
                if session_limit and data.startswith(b'PLAY ') and slot_name is None:
                    full = sessions.get(stream_name, 0) >= session_limit
                    if full or fail_available:
                        session_denials += 1
                        if not full:
                            fail_available -= 1
                        # Reproduce cameras which accept setup but close a new
                        # playback session when their reader budget is full.
                        return
                    slot_name = stream_name
                    sessions[slot_name] = sessions.get(slot_name, 0) + 1
                    if stall_available:
                        hold_media = True
                        stall_available = False
                        stalled_sessions += 1
                upstream.write(data)
                await upstream.drain()
            upstream.close()

        task = asyncio.create_task(requests())
        task.add_done_callback(lambda _: upstream.close())
        while True:
            first = await source.readexactly(1)
            if first == b'$':
                header = await source.readexactly(3)
                payload = await source.readexactly(int.from_bytes(header[1:], 'big'))
                if (connection['frozen'] and len(payload) >= 12 and payload[1] & 127 == 96):
                    frozen_video_packets += 1
                    continue
                if stalled or stream_name == stalled_path or time.monotonic() < delay_until or hold_media:
                    dropped += 1
                    continue
                data = first + header + payload
            else:
                header = first + await source.readuntil(b'\r\n\r\n')
                length = next((int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                               if line.lower().startswith(b'content-length:')), 0)
                data = header + await source.readexactly(length)
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        video_connections.pop(id(writer), None)
        if counted_name is not None:
            active_streams[counted_name] -= 1
        if slot_name is not None:
            sessions[slot_name] -= 1
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if upstream:
            upstream.close()
        writer.close()


async def main():
    rtsp = await asyncio.start_server(relay, '0.0.0.0', 8554)
    api = await asyncio.start_server(control, '0.0.0.0', 8080)
    async with rtsp, api:
        await asyncio.gather(rtsp.serve_forever(), api.serve_forever())


if __name__ == '__main__':
    asyncio.run(main())
