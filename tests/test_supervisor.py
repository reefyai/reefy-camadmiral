import os
import tempfile
import unittest
from pathlib import Path
from camadmiral.supervisor import render_go2rtc_config


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


if __name__ == "__main__":
    unittest.main()
