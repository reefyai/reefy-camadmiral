"""Keep persisted relay recovery assertions aligned with receive-only policy."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from e2e import faults


class RelayConfigurationPolicyTests(unittest.TestCase):
    def check_configuration(self, *, receive_only=True, stale=False):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "synthetic.db"
            config = Path(directory) / "go2rtc.yaml"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    "CREATE TABLE camera_identity_periods (camera_uuid TEXT,onvif_identity TEXT,ended_at TEXT);"
                    "CREATE TABLE cameras (camera_uuid TEXT,candidate_uuid TEXT);")
                connection.execute("INSERT INTO camera_identity_periods VALUES (?,?,NULL)",
                                   ("synthetic-identity", "urn:uuid:synthetic-onvif-camera"))
                connection.execute("INSERT INTO cameras VALUES (?,?)", ("synthetic-control", "candidate-open"))
            sources = {camera: [{"stream_key": camera + "_" + path,
                                "uri": "rtsp://" + host + ":8554/" + path,
                                "username": "operator", "password": "synthetic-password"}
                               for path in ("main", "sub")]
                       for camera, host in (("synthetic-identity", "172.30.0.15"),
                                            ("synthetic-control", "172.30.0.17"))}
            streams = {source["stream_key"]: [faults.authenticated_rtsp_uri(
                           source["uri"], source["username"], source["password"])
                           + ("#backchannel=0" if receive_only else "")]
                       for rows in sources.values() for source in rows}
            if stale:
                streams["synthetic-identity_main"][0] = streams["synthetic-identity_main"][0].replace("172.30.0.15", "172.30.0.13")
            config.write_text(yaml.safe_dump({"streams": streams}))
            with patch.object(faults, "DATABASE_PATH", database), \
                 patch.object(faults, "GO2RTC_CONFIG_PATH", config), \
                 patch.object(faults, "database_path", return_value=database), \
                 patch.object(faults, "load_master_key", return_value=b"k" * 32), \
                 patch.object(faults, "CameraRepository") as repository:
                repository.return_value.managed_stream_sources.side_effect = lambda *, camera_uuid: sources[camera_uuid]
                faults.assert_onvif_runtime_config_moved()

    def test_moved_runtime_configuration_requires_receive_only_sources(self):
        self.check_configuration()

    def test_missing_receive_only_policy_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self.check_configuration(receive_only=False)

    def test_stale_source_is_still_rejected_with_receive_only_policy(self):
        with self.assertRaises(RuntimeError):
            self.check_configuration(stale=True)


if __name__ == "__main__":
    unittest.main()
