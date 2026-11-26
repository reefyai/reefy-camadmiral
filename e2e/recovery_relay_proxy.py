"""Synthetic relay startup faults, through actual HTTP and RTSP sockets."""
import http.server
import json
import select
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request

LOCK = threading.Lock()
STATE = {'mode': 'none', 'restarts': 0, 'faults': 0, 'blocked_until': 0.0, 'armed': False}


class Api(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.server.server_port == 1986:
            with LOCK:body = json.dumps(STATE).encode()
            self.send_response(200);self.end_headers();self.wfile.write(body)
        else:self.forward()

    def do_POST(self):
        if self.server.server_port == 1986:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            with LOCK:
                STATE.update(mode=body['mode'], restarts=0, faults=0, blocked_until=0.0,
                             armed=body['mode'] != 'none')
            self.send_response(200);self.end_headers();self.wfile.write(b'{}')
        else:self.forward()

    def do_PATCH(self):self.forward()
    def do_PUT(self):self.forward()
    def do_DELETE(self):self.forward()

    def forward(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.command == 'POST' and self.path == '/api/restart':
            with LOCK:
                STATE['restarts'] += 1
                if STATE['armed']:
                    STATE['blocked_until'] = time.monotonic() + 9.0
                    if STATE['mode'] == 'transient':STATE['armed'] = False
        request = urllib.request.Request('http://127.0.0.1:1984'+self.path,
            data=body if body else None, method=self.command,
            headers={'Content-Type': self.headers.get('Content-Type','application/json')})
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
                status, payload, content_type = r.status, r.read(), r.headers.get('Content-Type')
        except urllib.error.HTTPError as error:
            status, payload, content_type = error.code, error.read(), 'text/plain'
        except OSError:
            if self.command == 'POST' and self.path == '/api/restart':
                self.close_connection = True
                return
            status, payload, content_type = 503, b'Relay restarting', 'text/plain'
        self.send_response(status)
        if content_type:self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(payload)));self.end_headers()
        try:self.wfile.write(payload)
        except (BrokenPipeError,ConnectionResetError):pass

    def log_message(self,*_args):pass


class Rtsp(socketserver.BaseRequestHandler):
    def handle(self):
        with LOCK:
            mode=STATE['mode'];blocked=STATE['blocked_until']
            fault = (mode == 'persistent' and STATE['restarts'] > 0) or time.monotonic() < blocked
            if fault:STATE['faults'] += 1
        if fault:
            if mode == 'persistent':
                try:self.request.recv(8192);self.request.sendall(b'RTSP/1.0 503 Service Unavailable\r\nCSeq: 1\r\n\r\n')
                except OSError:pass
                return
            time.sleep(max(0,blocked-time.monotonic()))
        try:
            with socket.create_connection(('127.0.0.1',18554),timeout=5) as upstream:
                sockets=[self.request,upstream]
                while True:
                    readable,_,_=select.select(sockets,[],[],15)
                    if not readable:return
                    for source in readable:
                        data=source.recv(65536)
                        if not data:return
                        (upstream if source is self.request else self.request).sendall(data)
        except OSError:return


if __name__ == '__main__':
    socketserver.ThreadingTCPServer.allow_reuse_address=True
    socketserver.ThreadingTCPServer.daemon_threads=True
    for port in (1985,1986):
        server=http.server.ThreadingHTTPServer(('0.0.0.0',port),Api)
        threading.Thread(target=server.serve_forever,daemon=True).start()
    with socketserver.ThreadingTCPServer(('0.0.0.0',18555),Rtsp) as server:server.serve_forever()
