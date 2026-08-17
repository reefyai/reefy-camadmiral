from __future__ import annotations

import asyncio


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    try:
        server_reader, server_writer = await asyncio.open_connection("172.30.0.30", 5000)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(
        relay(client_reader, server_writer),
        relay(server_reader, client_writer),
        return_exceptions=True,
    )


async def main() -> None:
    # The proxy shares CamAdmiral's network namespace. Listening on all
    # interfaces lets the isolated test driver inspect the same Frigate API
    # while CamAdmiral continues to use its production-safe loopback URL.
    server = await asyncio.start_server(handle, "0.0.0.0", 5000)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
