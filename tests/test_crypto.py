import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral.config import (
    CamAdmiralSettings,
    SecretConfigurationError,
    SecretSettings,
    StorageSettings,
)
from camadmiral.crypto import decrypt_password, encrypt_password, load_master_key


class CredentialCryptoTests(unittest.TestCase):
    def test_master_key_loads_from_mounted_base64_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = bytes(range(32))
            path = Path(directory) / "master-key"
            path.write_bytes(base64.urlsafe_b64encode(key) + b"\n")
            configured = CamAdmiralSettings(secrets=SecretSettings(master_key_file=path))
            with patch("camadmiral.crypto.settings", return_value=configured):
                self.assertEqual(load_master_key(), key)

    def test_password_round_trip_is_bound_to_credential_id(self) -> None:
        key = b"k" * 32
        encrypted = encrypt_password("synthetic-secret", "credential-1", key)

        self.assertEqual(decrypt_password(encrypted, "credential-1", key), "synthetic-secret")
        self.assertNotIn(b"synthetic-secret", encrypted)

    def test_empty_password_round_trip_supports_unauthenticated_rtsp(self) -> None:
        key = b"k" * 32
        encrypted = encrypt_password("", "credential-open-camera", key)

        self.assertEqual(len(encrypted), 29)
        self.assertEqual(
            decrypt_password(encrypted, "credential-open-camera", key),
            "",
        )

    def test_invalid_master_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master-key"
            path.write_text("too-short", encoding="utf-8")
            configured = CamAdmiralSettings(secrets=SecretSettings(master_key_file=path))
            with patch("camadmiral.crypto.settings", return_value=configured):
                with self.assertRaises(SecretConfigurationError):
                    load_master_key()

    def test_missing_default_key_is_generated_inside_data_volume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = CamAdmiralSettings(
                storage=StorageSettings(database=root / "camadmiral.db"),
                secrets=SecretSettings(master_key_file=root / "missing-mounted-key"),
            )
            with patch("camadmiral.crypto.settings", return_value=configured):
                first = load_master_key()
                second = load_master_key()

            generated = root / "secrets" / "master-key"
            self.assertEqual(len(first), 32)
            self.assertEqual(second, first)
            self.assertEqual(generated.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(generated.read_bytes().strip(), first)

    def test_missing_key_is_not_replaced_for_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "camadmiral.db"
            database.write_bytes(b"existing-state")
            configured = CamAdmiralSettings(
                storage=StorageSettings(database=database),
                secrets=SecretSettings(master_key_file=root / "missing-mounted-key"),
            )
            with patch("camadmiral.crypto.settings", return_value=configured):
                with self.assertRaisesRegex(SecretConfigurationError, "existing database"):
                    load_master_key()

            self.assertFalse((root / "secrets" / "master-key").exists())

    def test_explicit_missing_key_does_not_fall_back_to_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = CamAdmiralSettings(
                storage=StorageSettings(database=root / "camadmiral.db"),
                secrets=SecretSettings(
                    master_key_file=root / "required-mounted-key",
                    master_key_file_explicit=True,
                ),
            )
            with patch("camadmiral.crypto.settings", return_value=configured):
                with self.assertRaisesRegex(SecretConfigurationError, "does not exist"):
                    load_master_key()

            self.assertFalse((root / "secrets" / "master-key").exists())


if __name__ == "__main__":
    unittest.main()
