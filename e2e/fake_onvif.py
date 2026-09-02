from __future__ import annotations

import http.server
import os
import signal
import socket
import subprocess
import threading
import urllib.parse


ADDRESS = os.environ.get("ONVIF_ADDRESS", "172.30.0.13")
CAMERA_UUID = os.environ.get("ONVIF_UUID", "synthetic-onvif-camera")
CAMERA_NAME = os.environ.get("ONVIF_NAME", "Synthetic ONVIF")
MEDIA_CONFIG = os.environ.get("ONVIF_MEDIA_CONFIG", "/e2e/camera-onvif.yaml")
ONVIF_MULTICAST_GROUP = "239.255.255.250"
DEVICE_URL = f"http://{ADDRESS}:8080/onvif/device_service"
MEDIA_URL = f"http://{ADDRESS}:8080/onvif/media_service"
SOAP_START = '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'


def envelope(namespaces: str, body: str) -> bytes:
    return f'<?xml version="1.0"?>{SOAP_START} {namespaces}><s:Body>{body}</s:Body></s:Envelope>'.encode()


SERVICES = envelope(
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"',
    '<tds:GetServicesResponse><tds:Service><tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace>'
    f'<tds:XAddr>{MEDIA_URL}</tds:XAddr></tds:Service></tds:GetServicesResponse>',
)
PROFILES = envelope(
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"',
    '<trt:GetProfilesResponse>'
    '<trt:Profiles token="main"><tt:Name>Main stream</tt:Name><tt:VideoEncoderConfiguration>'
    '<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>1280</tt:Width><tt:Height>720</tt:Height></tt:Resolution>'
    '<tt:RateControl><tt:FrameRateLimit>10</tt:FrameRateLimit><tt:BitrateLimit>2048</tt:BitrateLimit></tt:RateControl>'
    '</tt:VideoEncoderConfiguration></trt:Profiles>'
    '<trt:Profiles token="sub"><tt:Name>Sub stream</tt:Name><tt:VideoEncoderConfiguration>'
    '<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>'
    '<tt:RateControl><tt:FrameRateLimit>5</tt:FrameRateLimit><tt:BitrateLimit>512</tt:BitrateLimit></tt:RateControl>'
    '</tt:VideoEncoderConfiguration></trt:Profiles></trt:GetProfilesResponse>',
)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if b"GetServices" in payload:
            response = SERVICES
        elif b"GetProfiles" in payload:
            response = PROFILES
        elif b"GetStreamUri" in payload:
            token = "sub" if b">sub<" in payload else "main"
            response = envelope(
                'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"',
                f'<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://{ADDRESS}:8554/{token}</tt:Uri>'
                '</trt:MediaUri></trt:GetStreamUriResponse>',
            )
        else:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def discovery_responder(stop: threading.Event) -> None:
    response = envelope(
        'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"',
        f'<d:ProbeMatches><d:ProbeMatch><a:EndpointReference><a:Address>urn:uuid:{CAMERA_UUID}</a:Address>'
        '</a:EndpointReference><d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>'
        f'<d:Scopes>onvif://www.onvif.org/name/{urllib.parse.quote(CAMERA_NAME)} onvif://www.onvif.org/hardware/LabCam</d:Scopes>'
        f'<d:XAddrs>{DEVICE_URL}</d:XAddrs></d:ProbeMatch></d:ProbeMatches>',
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", 3702))
        # Real ONVIF cameras join the WS-Discovery group; without membership the
        # kernel drops multicast probes and only unicast probes are answered.
        server.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton(ONVIF_MULTICAST_GROUP) + socket.inet_aton(ADDRESS),
        )
        server.settimeout(0.5)
        while not stop.is_set():
            try:
                _request, sender = server.recvfrom(65535)
            except socket.timeout:
                continue
            server.sendto(response, sender)


def main() -> int:
    stop = threading.Event()
    media = subprocess.Popen(["/usr/local/bin/go2rtc", "-config", MEDIA_CONFIG])
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    threading.Thread(target=discovery_responder, args=(stop,), daemon=True).start()
    signal.signal(
        signal.SIGTERM,
        lambda *_args: threading.Thread(target=server.shutdown, daemon=True).start(),
    )
    try:
        server.serve_forever()
    finally:
        stop.set()
        media.terminate()
        media.wait(timeout=5)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
