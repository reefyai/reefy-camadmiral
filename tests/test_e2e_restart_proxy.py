"""Restart-reply faults must not silently disable ordinary camera controls."""
import copy
import io
import json
import unittest
from unittest.mock import Mock, patch

from e2e.frigate_restart_proxy import Handler, STATE


class RestartProxyTests(unittest.TestCase):
    def setUp(self):
        self.before = copy.deepcopy(STATE)
        self.addCleanup(lambda: (STATE.clear(), STATE.update(self.before)))
        STATE.update(unavailable_until=0, runtime_controls=True)

    def handler(self, path, body=None):
        h = object.__new__(Handler)
        data = json.dumps(body).encode() if body is not None else b''
        h.path, h.command = path, 'POST' if body is not None else 'GET'
        h.headers = {'Content-Length': str(len(data))}
        h.rfile, h.reply = io.BytesIO(data), Mock()
        h.websocket_tunnel = Mock()
        return h

    def test_normal_websocket_uses_real_frigate_controls(self):
        h = self.handler('/ws')
        h.headers['Upgrade'] = 'websocket'
        with patch('e2e.frigate_restart_proxy.urllib.request.urlopen') as http:
            h.handle_request()
        h.websocket_tunnel.assert_called_once()
        http.assert_not_called()

    def test_restart_reply_fault_keeps_runtime_controls_available(self):
        h = self.handler('/__fault__/arm', {'mode': 'empty', 'restart_delays': [12]})
        h.handle_request()
        self.assertTrue(STATE['runtime_controls'])
        self.assertTrue(STATE['armed'])
        self.assertEqual('empty', STATE['mode'])

    def test_unavailable_control_is_still_an_explicit_fault(self):
        h = self.handler('/__fault__/arm', {'runtime_controls': False, 'restart_delays': []})
        h.handle_request()
        self.assertFalse(STATE['runtime_controls'])
