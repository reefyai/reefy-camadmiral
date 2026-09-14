"""Synthetic RTSP fault: keep control replies working but discard media packets."""
import asyncio

stalled = False
dropped = 0


async def control(reader, writer):
    global stalled
    line = await reader.readline()
    path = line.split()[1]
    if path == b'/stall':
        stalled = True
    elif path == b'/resume':
        stalled = False
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
    try:
        source, upstream = await asyncio.open_connection('camera-open', 8554)

        async def requests():
            while data := await reader.read(65536):
                upstream.write(data)
                await upstream.drain()
            upstream.close()

        task = asyncio.create_task(requests())
        while True:
            first = await source.readexactly(1)
            if first == b'$':
                header = await source.readexactly(3)
                payload = await source.readexactly(int.from_bytes(header[1:], 'big'))
                if stalled:
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
