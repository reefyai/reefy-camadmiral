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
        self.assertEqual(configured.integrations.frigate.targets, ())

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
                "integrations": {
                    "frigate": {
                        "targets": [
                            {
                                "id": "frigate-primary",
                                "name": "Primary Frigate",
                                "api_url": "http://127.0.0.1:20001/",
                                "sync_cameras": True,
                            },
                            {
                                "id": "frigate-secondary",
                                "name": "Secondary Frigate",
                                "api_url": "http://localhost:20007",
                                "sync_cameras": False,
                            },
                        ],
                        "default_target": "frigate-primary",
                    }
                },
            }
        )

        self.assertEqual(configured.server.port, 19090)
        self.assertEqual(configured.storage.database, Path("/srv/camadmiral/state.db"))
        self.assertEqual(configured.storage.inventory, Path("/srv/camadmiral/inventory.json"))
        self.assertEqual(configured.secrets.master_key_file, Path("/secrets/master"))
        self.assertTrue(configured.secrets.master_key_file_explicit)
        self.assertEqual(configured.secrets.admin_password_file, Path("/secrets/admin"))
        self.assertEqual(len(configured.integrations.frigate.targets), 2)
        self.assertTrue(configured.integrations.frigate.targets[0].sync_cameras)
        self.assertEqual(
            configured.integrations.frigate.targets[0].api_url,
            "http://127.0.0.1:20001",
        )

    def test_non_loopback_frigate_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "loopback"):
            parse_settings(
                {
                    "version": 1,
                    "integrations": {
                        "frigate": {
                            "targets": [
                                {
                                    "id": "frigate-primary",
                                    "name": "Primary Frigate",
                                    "api_url": "http://192.0.2.10:5000",
                                }
                            ]
                        }
                    },
                }
            )

    def test_multiple_targets_require_explicit_default(self) -> None:
        targets = [
            {"id": "one", "name": "One", "api_url": "http://127.0.0.1:20001"},
            {"id": "two", "name": "Two", "api_url": "http://127.0.0.1:20002"},
        ]
        with self.assertRaisesRegex(ConfigurationError, "require default_target"):
            parse_settings(
                {
                    "version": 1,
                    "integrations": {"frigate": {"targets": targets}},
                }
            )

    def test_unknown_settings_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "Unknown server setting"):
            parse_settings({"version": 1, "server": {"mystery": True}})


if __name__ == "__main__":
    unittest.main()
