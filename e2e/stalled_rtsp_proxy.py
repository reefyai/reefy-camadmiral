"""Synthetic RTSP fault: keep control replies working but discard media packets."""
import asyncio
import re
import json
import time
from urllib.parse import urlsplit, parse_qs

stalled = False
dropped = 0
stalled_path = None
delay_until = 0.0
rejected_path = None
auth_requests = 0
active_streams = {}
describes = {}


async def control(reader, writer):
    global stalled, stalled_path, delay_until, rejected_path
    line = await reader.readline()
    path = line.split()[1]
    if path == b'/stall':
        stalled = True
    elif path == b'/resume':
        stalled = False
        stalled_path = None
        delay_until = 0.0
        rejected_path = None
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
        body = json.dumps({'active': active_streams, 'describes': describes}).encode()
    writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: '
                 + str(len(body)).encode() + b'\r\n\r\n' + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def relay(reader, writer):
    global dropped
    upstream = None
    task = None
    stream_name = ''
    counted_name = None
    try:
        source, upstream = await asyncio.open_connection('camera-open', 8554)

        async def requests():
            nonlocal stream_name, counted_name
            global auth_requests
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
                if stalled or stream_name == stalled_path or time.monotonic() < delay_until:
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
        if counted_name is not None:
            active_streams[counted_name] -= 1
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
