"""Real child-process cleanup, without requiring a camera or FFmpeg installation."""
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from camadmiral import media


class SnapshotProcessTests(unittest.TestCase):
    def child(self, script):
        real_popen = subprocess.Popen
        self.children = []
        self.commands = []

        def launch(command, **kwargs):
            self.commands.append(command)
            child = real_popen([sys.executable, '-c', script], **kwargs)
            self.children.append(child)
            return child

        return patch.object(media.subprocess, 'Popen', side_effect=launch)

    def assert_reaped(self):
        self.assertEqual(len(self.children), 1)
        self.assertIsNotNone(self.children[0].returncode)
        self.assertTrue(self.children[0].stdout.closed)

    def test_decoder_success_is_single_frame_thread_bounded_and_reaped(self):
        with self.child("import sys; sys.stdout.buffer.write(b'\\xff\\xd8\\xfftest\\xff\\xd9')"):
            jpeg = media._snapshot_jpeg('rtsp://synthetic.invalid/stream', 320, time.monotonic() + 3)
        self.assertEqual(jpeg, b'\xff\xd8\xfftest\xff\xd9')
        self.assert_reaped()
        command = self.commands[0]
        for option in ('-threads', '-threads:v', '-filter_threads', '-filter_complex_threads', '-frames:v'):
            self.assertEqual(command[command.index(option) + 1], '1')
        self.assertEqual(command[command.index('-allowed_media_types') + 1], 'video')

    def test_stalled_decoder_is_killed_and_reaped(self):
        started = time.monotonic()
        with self.child('import time; time.sleep(60)'):
            with self.assertRaises(media.SnapshotError):
                media._snapshot_jpeg('rtsp://synthetic.invalid/stream', 320, started + .2)
        self.assert_reaped()
        self.assertLess(time.monotonic() - started, 2)
        self.assertLess(self.children[0].returncode, 0)

    def test_oversized_output_kills_and_reaps_decoder(self):
        script = "import sys,time; sys.stdout.buffer.write(b'x'*4096); sys.stdout.flush(); time.sleep(60)"
        with self.child(script), patch.object(media, 'SNAPSHOT_MAX_BYTES', 1024):
            with self.assertRaisesRegex(media.SnapshotError, 'too large'):
                media._snapshot_jpeg('rtsp://synthetic.invalid/stream', 320, time.monotonic() + 3)
        self.assert_reaped()
        self.assertLess(self.children[0].returncode, 0)

    def test_nonzero_exit_does_not_expose_command_or_credentials(self):
        with self.child("import sys; sys.stderr.write('synthetic-secret'); sys.exit(1)"):
            with self.assertRaisesRegex(media.SnapshotError, '^Snapshot is unavailable$'):
                media._snapshot_jpeg('rtsp://user:synthetic-secret@synthetic.invalid/stream', 320,
                                     time.monotonic() + 3)
        self.assert_reaped()

    def test_full_queue_does_not_spawn_decoder(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch.object(media, '_SNAPSHOT_SLOTS', slots), patch.object(media, 'SNAPSHOT_TIMEOUT', .05), \
             patch.object(media, '_snapshot_jpeg') as decoder:
            with self.assertRaisesRegex(media.SnapshotError, 'busy'):
                media.snapshot_frame('stream_test', rtsp_password='synthetic')
            decoder.assert_not_called()
        slots.release()

    def test_failures_release_shared_slots_under_concurrency(self):
        lock = threading.Lock()
        active = peak = 0
        slots = threading.BoundedSemaphore(2)

        def decode(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.02)
            with lock:
                active -= 1
            raise OSError('synthetic failure')

        def request(_):
            with self.assertRaisesRegex(media.SnapshotError, '^Snapshot is unavailable$'):
                media.snapshot_frame('stream_test', rtsp_password='synthetic')

        with patch.object(media, '_SNAPSHOT_SLOTS', slots), patch.object(media, '_snapshot_jpeg', side_effect=decode):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(request, range(8)))
        self.assertEqual(peak, 2)
        self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()
        slots.release()
