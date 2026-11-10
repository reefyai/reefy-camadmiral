"""Synthetic Frigate API proxy that drops one accepted restart reply.

Only the isolated import lab runs this fault injector. Request bodies and
camera sources are never logged. Control endpoints are not part of the app.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import socket
import threading
import time
import urllib.error
import urllib.request


LOCK = threading.Lock()
STATE = {'armed': False, 'unavailable_until': 0.0, 'dropped_replies': 0,
         'unavailable_reads': 0, 'mode': 'disconnect', 'restart_delays': [12],
         'restart_requests': 0, 'restore_outage_seconds': 0, 'restore_outages': 0,
         'runtime_delete_fault': None, 'runtime_delete_requests': 0, 'runtime_controls': False}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, code, body):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def websocket_tunnel(self):
        # Preserve real Frigate runtime controls in the grouped-import lab.
        # Other recovery scenarios deliberately keep their old fallback path.
        self.close_connection = True
        with socket.create_connection(('frigate', 5000), timeout=30) as upstream:
            request = (self.requestline + '\r\n' + ''.join(
                name + ': ' + value + '\r\n' for name, value in self.headers.items()) + '\r\n')
            upstream.sendall(request.encode('latin-1'))
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 35)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)

    def handle_request(self):
        size = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(size) if size else None
        if self.path.startswith('/__fault__/'):
            with LOCK:
                if self.path == '/__fault__/arm' and self.command == 'POST':
                    options = json.loads(body or b'{}')
                    mode = options.get('mode', 'disconnect')
                    assert mode in {'disconnect', 'partial', 'empty', 'ack'}
                    delays = options.get('restart_delays', [12])
                    assert all(0 <= value <= 240 for value in delays)
                    delete_fault = options.get('runtime_delete_fault')
                    assert delete_fault in {None, 'before', 'after'}
                    STATE.update(runtime_controls=options.get('runtime_controls', False), runtime_delete_fault=delete_fault, runtime_delete_requests=0, armed=bool(delays), dropped_replies=0, unavailable_reads=0, mode=mode,
                                 restart_delays=list(delays), restart_requests=0, restore_outages=0,
                                 restore_outage_seconds=options.get('restore_outage_seconds', 0))
                status = {k: v for k, v in STATE.items() if k != 'unavailable_until'}
                status['api_unavailable'] = time.monotonic() < STATE['unavailable_until']
            self.reply(200, json.dumps(status).encode())
            return
        with LOCK:
            restoring = (self.command == 'PUT' and self.path == '/api/config/set'
                         and json.loads(body or b'{}').get('config_data', {}).get('cameras', {})
                         .get('failure_camera', {}).get('enabled') is True)
            if restoring and STATE['restore_outage_seconds']:
                STATE.update(unavailable_until=time.monotonic() + STATE['restore_outage_seconds'],
                             restore_outage_seconds=0, restore_outages=STATE['restore_outages'] + 1)
            unavailable = time.monotonic() < STATE['unavailable_until']
            if unavailable:
                STATE['unavailable_reads'] += 1
        if unavailable:
            self.reply(503, b'{"message":"Synthetic restart still in progress"}')
            return
        if self.headers.get('Upgrade', '').lower() == 'websocket' and STATE['runtime_controls']:
            self.websocket_tunnel()
            return
        runtime_delete = self.command == 'DELETE' and self.path.startswith('/api/go2rtc/streams/')
        with LOCK:
            delete_fault = STATE['runtime_delete_fault']
            if runtime_delete:
                STATE['runtime_delete_requests'] += 1
        if runtime_delete and delete_fault == 'before':
            self.reply(400, b'{"success":false,"message":"Failed to delete stream: yaml: path not exist"}')
            return
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in {'host', 'content-length', 'connection'}}
        request = urllib.request.Request('http://frigate:5000' + self.path,
                                         data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                code, payload = response.status, response.read()
        except urllib.error.HTTPError as exc:
            code, payload = exc.code, exc.read()
        except (OSError, urllib.error.URLError):
            self.reply(503, b'{"message":"Synthetic upstream restarting"}')
            return
        if runtime_delete and delete_fault == 'after':
            # The real deletion has happened; fail only the persistence reply.
            self.reply(400, b'{"success":false,"message":"Failed to delete stream: yaml: path not exist"}')
            return
        with LOCK:
            if self.command == 'POST' and self.path == '/api/restart' and code == 200:
                STATE['restart_requests'] += 1
            drop = (STATE['armed'] and self.command == 'POST'
                    and self.path == '/api/restart' and code == 200)
            if drop:
                delay = STATE['restart_delays'].pop(0)
                STATE.update(armed=bool(STATE['restart_delays']), unavailable_until=time.monotonic() + delay,
                             dropped_replies=STATE['dropped_replies'] + (STATE['mode'] != 'ack'))
        if drop and STATE['mode'] != 'ack':
            mode = STATE['mode']
            print('Interrupted an accepted restart reply (' + mode + '); holding API unavailable for 12 seconds.', flush=True)
            if mode in {'partial', 'empty'}:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload) + 10) if mode == 'partial' else '0')
                self.end_headers()
                if mode == 'partial':
                    self.wfile.write(payload[:max(1, len(payload) // 2)])
                    self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.reply(code, payload)

    do_GET = do_POST = do_PUT = do_DELETE = handle_request


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 20017), Handler).serve_forever()
