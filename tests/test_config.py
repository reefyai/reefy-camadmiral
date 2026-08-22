import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral.config import (
    CONFIG_FILE_ENV,
    CamAdmiralSettings,
    ConfigurationError,
    parse_settings,
    reset_settings_cache,
    settings,
)


class ConfigurationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_settings_cache()

    def test_missing_default_file_uses_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "not-present.yaml"
            with patch.dict(os.environ, {}, clear=True), patch(
                "camadmiral.config.DEFAULT_CONFIG_FILE", missing
            ):
                reset_settings_cache()
                configured = settings()

        self.assertEqual(configured, CamAdmiralSettings())

    def test_empty_explicit_file_uses_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camadmiral.yaml"
            path.write_text("\n", encoding="utf-8")
            with patch.dict(os.environ, {CONFIG_FILE_ENV: str(path)}, clear=True):
                reset_settings_cache()
                configured = settings()

        self.assertEqual(configured, CamAdmiralSettings())

    def test_missing_explicit_file_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "not-present.yaml"
            with patch.dict(os.environ, {CONFIG_FILE_ENV: str(missing)}, clear=True):
                reset_settings_cache()
                with self.assertRaisesRegex(ConfigurationError, "does not exist"):
                    settings()

    def test_full_configuration_is_normalized(self) -> None:
        configured = parse_settings(
            {
                "version": 1,
                "server": {"listen": "127.0.0.1", "port": 19090},
                "storage": {
                    "database": "/srv/camadmiral/state.db",
                },
                "secrets": {
                    "master_key_file": "/secrets/master",
                    "api_token_file": "/secrets/api",
                    "admin_password_file": "/secrets/admin",
                },
            }
        )

        self.assertEqual(configured.server.port, 19090)
        self.assertEqual(configured.storage.database, Path("/srv/camadmiral/state.db"))
        self.assertEqual(configured.storage.inventory, Path("/srv/camadmiral/inventory.json"))
        self.assertEqual(configured.secrets.master_key_file, Path("/secrets/master"))
        self.assertTrue(configured.secrets.master_key_file_explicit)
        self.assertEqual(configured.secrets.admin_password_file, Path("/secrets/admin"))
    def test_frigate_is_not_a_file_configuration_setting(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "Unknown top-level setting: integrations"):
            parse_settings({"version": 1, "integrations": {"frigate": {}}})

    def test_unknown_settings_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "Unknown server setting"):
            parse_settings({"version": 1, "server": {"mystery": True}})


if __name__ == "__main__":
    unittest.main()
