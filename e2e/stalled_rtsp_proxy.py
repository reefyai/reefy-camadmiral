"""Synthetic RTSP fault: keep control replies working but discard media packets."""
import asyncio
import re
import time
from urllib.parse import urlsplit, parse_qs

stalled = False
dropped = 0
stalled_path = None
delay_until = 0.0


async def control(reader, writer):
    global stalled, stalled_path, delay_until
    line = await reader.readline()
    path = line.split()[1]
    if path == b'/stall':
        stalled = True
    elif path == b'/resume':
        stalled = False
        stalled_path = None
        delay_until = 0.0
    elif path.startswith(b'/stall/'):
        stalled_path = path.removeprefix(b'/stall/').decode()
    elif path.startswith(b'/delay?'):
        seconds = float(parse_qs(urlsplit(path.decode()).query)['seconds'][0])
        delay_until = time.monotonic() + max(0, min(60, seconds))
    body = f'stalled={stalled} dropped={dropped}'.encode()
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
    try:
        source, upstream = await asyncio.open_connection('camera-open', 8554)

        async def requests():
            nonlocal stream_name
            while data := await reader.read(65536):
                match = re.search(rb'(?:DESCRIBE|PLAY) (rtsp://[^\s]+)', data)
                if match:
                    stream_name = urlsplit(match.group(1).decode()).path.strip('/')
                upstream.write(data)
                await upstream.drain()
            upstream.close()

        task = asyncio.create_task(requests())
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
