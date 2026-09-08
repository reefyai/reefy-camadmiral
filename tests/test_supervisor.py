import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from camadmiral.supervisor import (
    Child,
    notify_relay_restart,
    redact_log_credentials,
    render_go2rtc_config,
)


class SupervisorTests(unittest.TestCase):
    def test_go2rtc_template_uses_extendable_stream_block(self) -> None:
        template = Path(__file__).parents[1] / "go2rtc.yaml"
        self.assertIn("\nstreams:\n", template.read_text())
        self.assertNotIn("streams: {}", template.read_text())

    def test_media_access_binds_lan_only_with_password(self) -> None:
        template = Path(__file__).parents[1].joinpath("go2rtc.yaml").read_text()
        configured = render_go2rtc_config(template, "synthetic-media-secret")
        unconfigured = render_go2rtc_config(template, None)

        self.assertIn('listen: "0.0.0.0:18554"', configured)
        self.assertIn('password: "synthetic-media-secret"', configured)
        self.assertIn('listen: "127.0.0.1:18554"', unconfigured)

    def test_runtime_config_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "runtime.yaml"
            target.write_text("streams:\n")
            os.chmod(target, 0o600)

            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_unexpected_relay_restart_queues_channel_neutral_event(self) -> None:
        repository = Mock()

        notify_relay_restart(
            repository,
            reason="unexpected_process_exit",
            camera_count=0,
        )

        repository.enqueue_relay_restart_notification.assert_called_once_with(
            reason="unexpected_process_exit",
            camera_count=0,
        )

    def test_notification_failure_does_not_stop_relay_supervision(self) -> None:
        repository = Mock()
        repository.enqueue_relay_restart_notification.side_effect = RuntimeError(
            "synthetic notification failure"
        )

        notify_relay_restart(
            repository,
            reason="unexpected_process_exit",
            camera_count=0,
        )

    def test_go2rtc_log_credentials_are_redacted_without_losing_context(self) -> None:
        line = (
            'WRN error="read timeout" '
            "url=rtsp://operator:synthetic-secret@192.0.2.25:554/live "
            "fallback=http://192.0.2.30/live?username=operator&password=other-secret "
            "stream=stream_synthetic"
        )

        redacted = redact_log_credentials(line)

        self.assertIn("WRN", redacted)
        self.assertIn("read timeout", redacted)
        self.assertIn("rtsp://***@192.0.2.25:554/live", redacted)
        self.assertIn("username=***&password=***", redacted)
        self.assertIn("stream=stream_synthetic", redacted)
        self.assertNotIn("operator", redacted)
        self.assertNotIn("synthetic-secret", redacted)
        self.assertNotIn("other-secret", redacted)

    @patch("camadmiral.supervisor.threading.Thread")
    @patch("camadmiral.supervisor.subprocess.Popen")
    def test_go2rtc_child_routes_combined_output_through_redactor(
        self,
        popen,
        thread,
    ) -> None:
        process = Mock()
        process.stdout = Mock()
        popen.return_value = process

        Child("go2rtc", ["go2rtc"], redact_logs=True).start()

        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertTrue(kwargs["text"])
        self.assertEqual(thread.call_args.kwargs["args"], (process.stdout,))
        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
