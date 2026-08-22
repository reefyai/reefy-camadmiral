import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from camadmiral.config import CamAdmiralSettings, SecretSettings, StorageSettings
from camadmiral.crypto import load_master_key
from camadmiral.media import ProbeResult
from camadmiral.storage import CameraRepository


class BackupRestoreTests(unittest.TestCase):
    def _settings(self, root: Path) -> CamAdmiralSettings:
        return CamAdmiralSettings(
            storage=StorageSettings(database=root / "camadmiral.db"),
            secrets=SecretSettings(master_key_file=root / "external-key-not-configured"),
        )

    def _load_generated_key(self, root: Path) -> bytes:
        with patch("camadmiral.crypto.settings", return_value=self._settings(root)):
            return load_master_key()

    def test_complete_data_volume_round_trip_preserves_operational_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source_root = workspace / "source-volume"
            source_root.mkdir()
            source_key = self._load_generated_key(source_root)
            source_repository = CameraRepository(source_root / "camadmiral.db", source_key)
            source_repository.migrate()
            adoption = source_repository.adopt(
                {
                    "candidate_uuid": "candidate-backup",
                    "display_name": "Synthetic entrance",
                    "ip": "192.0.2.10",
                },
                "operator",
                "synthetic-upstream-secret",
                [
                    {
                        "token": "main",
                        "name": "Main",
                        "uri": "rtsp://192.0.2.10/main",
                        "width": 1920,
                        "height": 1080,
                        "encoding": "H264",
                        "fps": 20,
                        "bitrate_kbps": 4096,
                    },
                    {
                        "token": "sub",
                        "name": "Sub",
                        "uri": "rtsp://192.0.2.10/sub",
                        "width": 640,
                        "height": 360,
                        "encoding": "H264",
                        "fps": 10,
                        "bitrate_kbps": 512,
                    },
                ],
                {"record": "main", "detect": "sub"},
            )
            stream_ids = {
                stream["profile_token"]: stream["stream_uuid"]
                for stream in adoption["streams"]
            }
            source_repository.record_probe_results(
                {
                    stream_ids["main"]: ProbeResult("ready", 20, "h264", "aac", 1920, 1080, 20),
                    stream_ids["sub"]: ProbeResult("ready", 15, "h264", None, 640, 360, 10),
                }
            )
            source_access_password = source_repository.rtsp_access_password()
            sources = source_repository.managed_stream_sources()
            revision_id, revision_status = source_repository.record_desired_media_revision(sources)
            if revision_status == "desired":
                source_repository.complete_media_revision(revision_id, "applied")
            source_repository.save_frigate_target(
                "frigate-synthetic",
                "Synthetic Frigate",
                "http://127.0.0.1:20001",
                sync_cameras=True,
            )
            source_repository.record_frigate_target_check(
                "frigate-synthetic",
                status="connected",
            )
            source_repository.record_frigate_attempt(
                "frigate-synthetic",
                adoption["camera_uuid"],
                "camadmiral_synthetic",
                stream_ids["main"],
                stream_ids["sub"],
                "synthetic-desired-hash",
            )
            source_repository.complete_frigate_attempt(
                "frigate-synthetic",
                adoption["camera_uuid"],
                status="applied",
                applied_hash="synthetic-desired-hash",
            )
            source_repository.record_address_change(
                adoption["camera_uuid"],
                "192.0.2.9",
                "192.0.2.10",
                "unique-mac",
            )
            inventory = {
                "devices": [
                    {
                        "candidate_uuid": "candidate-backup",
                        "display_name": "Synthetic entrance",
                        "ip": "192.0.2.10",
                        "status": "online",
                    }
                ]
            }
            (source_root / "inventory.json").write_text(
                json.dumps(inventory),
                encoding="utf-8",
            )

            backup_root = workspace / "backup"
            restored_root = workspace / "restored-volume"
            shutil.copytree(source_root, backup_root)
            shutil.copytree(backup_root, restored_root)

            for path in backup_root.rglob("*"):
                if path.is_file():
                    payload = path.read_bytes()
                    self.assertNotIn(b"synthetic-upstream-secret", payload)
                    self.assertNotIn(source_access_password.encode(), payload)

            restored_key = self._load_generated_key(restored_root)
            restored_repository = CameraRepository(restored_root / "camadmiral.db", restored_key)
            restored_repository.migrate()
            restored_adoption = restored_repository.adoption_for_candidate("candidate-backup")

            self.assertEqual(restored_key, source_key)
            self.assertEqual(
                (restored_root / "secrets" / "master-key").stat().st_mode & 0o777,
                0o600,
            )
            self.assertEqual(restored_adoption["camera_uuid"], adoption["camera_uuid"])
            self.assertEqual(
                {
                    stream["profile_token"]: stream["stream_uuid"]
                    for stream in restored_adoption["streams"]
                },
                stream_ids,
            )
            self.assertEqual(restored_adoption["roles"], adoption["roles"])
            self.assertEqual(
                restored_repository.credentials_for_candidate("candidate-backup"),
                ("operator", "synthetic-upstream-secret"),
            )
            self.assertEqual(restored_repository.rtsp_access_password(), source_access_password)
            self.assertEqual(
                restored_repository.last_known_good_media_revision()["config"],
                source_repository.last_known_good_media_revision()["config"],
            )
            self.assertEqual(
                restored_repository.frigate_binding(
                    "frigate-synthetic",
                    adoption["camera_uuid"],
                )["status"],
                "applied",
            )
            self.assertEqual(
                restored_repository.frigate_target("frigate-synthetic")["connection_status"],
                "connected",
            )
            with restored_repository.connect() as connection:
                address_events = connection.execute(
                    "SELECT count(*) FROM camera_address_events WHERE camera_uuid = ?",
                    (adoption["camera_uuid"],),
                ).fetchone()[0]
            self.assertEqual(address_events, 1)
            self.assertEqual(
                json.loads((restored_root / "inventory.json").read_text(encoding="utf-8")),
                inventory,
            )


if __name__ == "__main__":
    unittest.main()
