"""Per-target media policy migration, ownership and operator-data regressions."""
import copy
import unittest
from unittest.mock import patch

import test_frigate_import as fixtures
from camadmiral.frigate import desired_camera, selected_camera_inventory, frigate_camera_configuration
from camadmiral.frigate_import import ImportStore, sync_imported


class FrigateAudioTests(unittest.TestCase):
    def setUp(self):
        self.h = fixtures.FrigateImportTests(methodName="runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.repo, self.target, self.client = self.h.repo, self.h.target, self.h.client

    def policy(self, mode):
        self.repo.save_frigate_target(self.target.target_id, self.target.name,
                                      self.target.api_url, audio_mode=mode)

    def test_new_targets_default_video_only_and_metadata_edits_keep_policy(self):
        self.assertEqual("video_only", self.repo.frigate_target(self.target.target_id)["audio_mode"])
        self.policy("preserve")
        self.repo.save_frigate_target(self.target.target_id, "Renamed", self.target.api_url)
        self.assertEqual("preserve", self.repo.frigate_targets()[0]["audio_mode"])
        with self.assertRaises(ValueError):
            self.policy("invalid")
        self.assertEqual("preserve", self.repo.frigate_targets()[0]["audio_mode"])

    def test_pre_policy_database_migrates_to_preserve_idempotently(self):
        with self.repo.connect() as db:
            db.execute("ALTER TABLE frigate_targets DROP COLUMN audio_mode")
            db.commit()
        self.repo.migrate()
        self.repo.migrate()
        self.assertEqual("preserve", self.repo.frigate_target(self.target.target_id)["audio_mode"])
        self.repo.save_frigate_target("new-target", "New target", "http://other-frigate:5000")
        self.assertEqual("video_only", self.repo.frigate_target("new-target")["audio_mode"])

    def test_import_policy_switch_preserves_tuning_alias_ownership_and_restore(self):
        self.policy("preserve")
        job = self.h.execute(self.h.preview())
        self.assertEqual("applied", job["cameras"][0]["status"])
        store = ImportStore(self.repo)
        before = store.links(self.target.target_id)["legacy_entrance"]
        self.client.current_raw_config["cameras"]["legacy_entrance"]["detect"]["fps"] = 9
        camera = copy.deepcopy(self.client.raw_config()["cameras"]["legacy_entrance"])
        self.policy("video_only")
        self.client.operations.clear()
        with patch("camadmiral.frigate_import._refresh_capture") as refresh:
            sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda _: "192.0.2.20")
        after = store.links(self.target.target_id)["legacy_entrance"]
        self.assertEqual(camera, self.client.raw_config()["cameras"]["legacy_entrance"])
        self.assertEqual(before["camera_uuid"], after["camera_uuid"])
        self.assertEqual(before["original"], after["original"])
        self.assertEqual(before["aliases"], after["aliases"])
        self.assertTrue(all(v[0].endswith("?video") for v in after["managed_streams"].values()))
        refresh.assert_not_called()
        writes = len(self.client.config_writes)
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda _: "192.0.2.20")
        self.assertEqual(writes, len(self.client.config_writes), "Repeated sync rewrote managed sources")
        self.policy("preserve")
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda _: "192.0.2.20")
        self.assertEqual(before["managed_streams"], store.links(self.target.target_id)["legacy_entrance"]["managed_streams"])

    def test_policy_never_overwrites_an_external_source_edit(self):
        self.policy("preserve")
        self.h.execute(self.h.preview())
        row = ImportStore(self.repo).links(self.target.target_id)["legacy_entrance"]
        alias = next(iter(row["managed_streams"]))
        self.client.current_raw_config["go2rtc"]["streams"][alias] = ["rtsp://operator.example/changed"]
        original = self.client.raw_config()
        self.policy("video_only")
        sync_imported(self.repo, self.target, client=self.client, host_resolver=lambda _: "192.0.2.20")
        self.assertEqual(original, self.client.raw_config())
        self.assertEqual("import_source_conflict", self.repo.frigate_camera_selections(self.target.target_id)[0]["sync_error"])

    def test_native_export_and_preview_follow_target_policy(self):
        job = self.h.execute(self.h.preview())
        uid = job["cameras"][0]["camera_uuid"]
        camera = selected_camera_inventory(self.repo, self.target.target_id)[0]
        video = desired_camera(camera, "synthetic+secret", "2001:db8::1", audio_mode="video_only")
        audio = desired_camera(camera, "synthetic+secret", "2001:db8::1", audio_mode="preserve")
        self.assertTrue(all(v[0].endswith("?video") for v in video["streams"].values()))
        self.assertTrue(all("[2001:db8::1]" in v[0] and "synthetic%2Bsecret" in v[0] for v in video["streams"].values()))
        self.assertNotEqual(video["desired_hash"], audio["desired_hash"])
        self.assertEqual(video["camera_config"], audio["camera_config"])
        preview = frigate_camera_configuration(self.repo, self.target, uid, media_host="192.0.2.20")
        self.assertIn("?video", preview["configuration"])

    def test_video_only_removes_audio_conversion_but_keeps_video_options(self):
        from camadmiral.frigate_import import _live_source
        url = "rtsp://relay.example:18554/stream_example?video"
        wrapper = "#video=copy#audio=aac#audio=opus#hardware=vaapi"
        self.assertEqual("ffmpeg:" + url + "#video=copy#hardware=vaapi", _live_source(url, wrapper, "video_only"))
        self.assertEqual("ffmpeg:" + url + wrapper, _live_source(url, wrapper, "preserve"))
        self.assertEqual(url, _live_source(url, "#audio=aac", "video_only"))
