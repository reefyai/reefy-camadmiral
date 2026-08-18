import json
import subprocess
import unittest
import urllib.error
from unittest.mock import Mock, patch

from camadmiral.media import (
    ProbeResult,
    RelayHealthMonitor,
    SnapshotError,
    authenticated_rtsp_uri,
    go2rtc_websocket_url,
    probe_source,
    probe_stream,
    probe_upstreams,
    reconcile_and_probe,
    reconcile_preloads,
    reconcile_runtime_drift,
    snapshot_frame,
)


class MediaTests(unittest.TestCase):
    @patch("camadmiral.media._request")
    def test_relay_health_uses_active_video_counters_without_opening_sources(self, request) -> None:
        runtime = {
            "stream_active": {
                "producers": [{
                    "id": 7,
                    "bytes_recv": 1000,
                    "receivers": [{
                        "codec": {"codec_name": "h264", "codec_type": "video"},
                        "packets": 20,
                    }],
                }],
                "consumers": [{"id": 9}],
            },
            "stream_idle": {
                "producers": [{"url": "rtsp://synthetic.invalid/idle"}],
                "consumers": [],
            },
        }
        request.side_effect = lambda _method, path, _query=None: json.dumps(
            {"stream_active": {}} if path == "/api/preload" else runtime
        ).encode()
        repository = Mock()
        sources = [
            {
                "stream_uuid": "active",
                "stream_key": "stream_active",
                "camera_uuid": "camera-active",
            },
            {
                "stream_uuid": "idle",
                "stream_key": "stream_idle",
                "camera_uuid": "camera-idle",
            },
        ]
        repository.managed_stream_sources.side_effect = [
            sources,
            [sources[0]],
            sources,
            [sources[0]],
            sources,
            [sources[0]],
        ]
        monitor = RelayHealthMonitor()

        first = monitor.probe(repository)
        second = monitor.probe(repository)
        runtime["stream_active"]["producers"][0]["receivers"][0]["packets"] = 21
        third = monitor.probe(repository)

        self.assertEqual(first["active"].status, "ready")
        self.assertEqual(first["idle"].status, "idle")
        self.assertEqual(second["active"].status, "unavailable")
        self.assertEqual(third["active"].status, "ready")
        repository.managed_stream_sources.assert_any_call(
            include_auth_failed=False,
            role_bound_only=True,
        )
        repository.managed_stream_sources.assert_any_call(
            include_auth_failed=False,
            bound_role="detect",
        )
        self.assertIn(("GET", "/api/streams"), [call.args for call in request.call_args_list])

    @patch("camadmiral.media.probe_source", return_value=ProbeResult("auth_failed", 20))
    @patch("camadmiral.media._request")
    def test_relay_diagnoses_auth_only_after_sustained_preloaded_failure(
        self,
        request,
        probe_source_mock,
    ) -> None:
        source = {
            "stream_uuid": "detect",
            "stream_key": "stream_detect",
            "camera_uuid": "camera-1",
            "uri": "rtsp://192.0.2.10/live",
            "username": "operator",
            "password": "synthetic-secret",
        }
        runtime = {
            "stream_detect": {
                "producers": [{"url": source["uri"]}],
                "consumers": [{"id": 9}],
            }
        }
        request.side_effect = lambda _method, path, _query=None: json.dumps(
            {"stream_detect": {}} if path == "/api/preload" else runtime
        ).encode()
        repository = Mock()
        repository.managed_stream_sources.return_value = [source]
        monitor = RelayHealthMonitor()

        monitor.probe(repository)
        monitor.probe(repository)

        probe_source_mock.assert_called_once_with(
            source["uri"],
            source["username"],
            source["password"],
        )
        repository.record_camera_auth_failure.assert_called_once_with(
            "camera-1",
            ProbeResult("auth_failed", 20),
        )

    @patch("camadmiral.media._request")
    def test_failed_camera_preload_does_not_block_other_cameras(self, request) -> None:
        def response(method, path, query=None):
            if method == "GET" and path == "/api/preload":
                return b"{}"
            if query and query.get("src") == "stream_offline":
                raise urllib.error.HTTPError(
                    "http://127.0.0.1/api/preload",
                    500,
                    "synthetic camera unavailable",
                    {},
                    None,
                )
            return b""

        request.side_effect = response

        reconcile_preloads([
            {"stream_key": "stream_offline"},
            {"stream_key": "stream_online"},
        ])

        self.assertIn(
            ("PUT", "/api/preload", {"src": "stream_online", "video": "all"}),
            [call.args for call in request.call_args_list],
        )

    @patch("camadmiral.media.GO2RTC_URL", "http://127.0.0.1:1984")
    def test_live_websocket_url_targets_only_managed_stream(self) -> None:
        self.assertEqual(
            go2rtc_websocket_url("stream_synthetic-value"),
            "ws://127.0.0.1:1984/api/ws?src=stream_synthetic-value",
        )
        with self.assertRaisesRegex(ValueError, "Invalid managed stream"):
            go2rtc_websocket_url("rtsp://camera.invalid/live")

    @patch("camadmiral.media.urllib.request.urlopen")
    def test_snapshot_reads_bounded_jpeg_from_managed_stream(self, urlopen) -> None:
        jpeg = b"\xff\xd8\xff\xe0synthetic-image\xff\xd9"
        response = Mock()
        response.headers.get_content_type.return_value = "image/jpeg"
        response.headers.get.return_value = str(len(jpeg))
        response.read.return_value = jpeg
        urlopen.return_value.__enter__.return_value = response

        result = snapshot_frame("stream_synthetic")

        self.assertEqual(result, jpeg)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "image/jpeg")
        self.assertIn("/api/frame.jpeg?", request.full_url)
        self.assertIn("src=stream_synthetic", request.full_url)
        self.assertIn("width=960", request.full_url)

    @patch("camadmiral.media.urllib.request.urlopen")
    def test_snapshot_rejects_non_jpeg_response(self, urlopen) -> None:
        response = Mock()
        response.headers.get_content_type.return_value = "text/plain"
        response.headers.get.return_value = None
        urlopen.return_value.__enter__.return_value = response

        with self.assertRaisesRegex(SnapshotError, "unexpected format"):
            snapshot_frame("stream_synthetic")

    @patch("camadmiral.media.urllib.request.urlopen")
    def test_snapshot_rejects_invalid_jpeg_data(self, urlopen) -> None:
        response = Mock()
        response.headers.get_content_type.return_value = "image/jpeg"
        response.headers.get.return_value = None
        response.read.return_value = b"not-a-jpeg"
        urlopen.return_value.__enter__.return_value = response

        with self.assertRaisesRegex(SnapshotError, "invalid JPEG"):
            snapshot_frame("stream_synthetic")

    def test_authenticated_uri_encodes_credentials(self) -> None:
        result = authenticated_rtsp_uri(
            "rtsp://192.0.2.10:554/media?channel=1",
            "camera user",
            "p@ss:/word",
        )

        self.assertEqual(
            result,
            "rtsp://camera%20user:p%40ss%3A%2Fword@192.0.2.10:554/media?channel=1",
        )

    @patch("camadmiral.media.subprocess.run")
    def test_probe_reports_empirical_video_metadata(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 1280,
                            "height": 720,
                            "avg_frame_rate": "15/1",
                        },
                        {"codec_type": "audio", "codec_name": "aac"},
                    ]
                }
            ).encode(),
            stderr=b"",
        )

        result = probe_stream("stream_synthetic", "camadmiral", "synthetic-media-secret")

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.video_codec, "h264")
        self.assertEqual(result.audio_codec, "aac")
        self.assertEqual((result.width, result.height, result.fps), (1280, 720, 15))
        command = run.call_args.args[0]
        self.assertEqual(
            command[-1],
            "rtsp://camadmiral:synthetic-media-secret@127.0.0.1:18554/stream_synthetic",
        )

    @patch("camadmiral.media.subprocess.run")
    def test_direct_source_probe_keeps_credentials_out_of_stored_url(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 640,
                            "height": 360,
                            "avg_frame_rate": "10/1",
                        }
                    ]
                }
            ).encode(),
            stderr=b"",
        )

        result = probe_source(
            "rtsp://192.0.2.10/media",
            "camera user",
            "synthetic-secret",
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(
            run.call_args.args[0][-1],
            "rtsp://camera%20user:synthetic-secret@192.0.2.10/media",
        )

    @patch("camadmiral.media.subprocess.run")
    def test_probe_failure_does_not_expose_subprocess_error(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"sensitive detail")

        result = probe_stream("stream_synthetic")

        self.assertEqual(result.status, "unavailable")

    @patch("camadmiral.media.subprocess.run")
    def test_probe_classifies_authentication_failure_without_exposing_error(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            [],
            1,
            stdout=b"",
            stderr=b"method DESCRIBE failed: 401 Unauthorized sensitive detail",
        )

        result = probe_source("rtsp://192.0.2.10/live", "operator", "synthetic-secret")

        self.assertEqual(result.status, "auth_failed")

    @patch("camadmiral.media._probe_source_entries")
    def test_upstream_auth_failure_marks_whole_camera_and_stops_more_probes(self, probe_entries) -> None:
        sources = [
            {
                "camera_uuid": "camera-1",
                "stream_uuid": "stream-1",
                "stream_key": "stream_one",
                "uri": "rtsp://192.0.2.10/main",
                "username": "operator",
                "password": "synthetic-secret",
            },
            {
                "camera_uuid": "camera-1",
                "stream_uuid": "stream-2",
                "stream_key": "stream_two",
                "uri": "rtsp://192.0.2.10/sub",
                "username": "operator",
                "password": "synthetic-secret",
            },
        ]
        repository = Mock()
        repository.managed_stream_sources.return_value = sources
        probe_entries.return_value = {"stream-1": ProbeResult("auth_failed", 50)}

        probe_upstreams(repository)

        probe_entries.assert_called_once_with([sources[0]])
        repository.record_camera_auth_failure.assert_called_once()
        repository.record_probe_results.assert_called_once_with({})

    @patch("camadmiral.media.probe_streams", return_value={})
    @patch("camadmiral.media.runtime_stream_keys", return_value={"stream_one"})
    @patch("camadmiral.media.reconcile_preloads")
    @patch("camadmiral.media.reconcile_streams")
    def test_reconcile_marks_desired_revision_applied(
        self,
        reconcile,
        reconcile_preloads_mock,
        _runtime_keys,
        _probe_streams,
    ) -> None:
        source = {
            "stream_uuid": "stream-1",
            "stream_key": "stream_one",
            "uri": "rtsp://192.0.2.10/live",
            "username": "operator",
            "password": "synthetic-secret",
            "credential_uuid": "credential-1",
        }
        repository = Mock()
        repository.managed_stream_sources.return_value = [source]
        repository.record_desired_media_revision.return_value = (7, "desired")
        repository.rtsp_access_password.return_value = "synthetic-media-secret"

        reconcile_and_probe(repository)

        reconcile.assert_called_once_with([source])
        reconcile_preloads_mock.assert_called_once_with([source])
        repository.complete_media_revision.assert_called_once_with(7, "applied")

    @patch("camadmiral.media.reconcile_streams", side_effect=RuntimeError("synthetic failure"))
    def test_reconcile_failure_does_not_promote_desired_revision(self, _reconcile) -> None:
        repository = Mock()
        repository.managed_stream_sources.return_value = []
        repository.record_desired_media_revision.return_value = (8, "desired")

        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            reconcile_and_probe(repository)

        repository.complete_media_revision.assert_called_once_with(
            8,
            "failed",
            "runtime_apply_failed",
        )

    @patch("camadmiral.media.reconcile_and_probe")
    @patch("camadmiral.media.runtime_stream_keys", return_value={"stream_one", "other_app"})
    def test_runtime_streams_are_left_unchanged_without_drift(
        self,
        _runtime_keys,
        reconcile,
    ) -> None:
        repository = Mock()
        repository.managed_stream_sources.return_value = [{"stream_key": "stream_one"}]

        changed = reconcile_runtime_drift(repository)

        self.assertFalse(changed)
        reconcile.assert_not_called()

    @patch("camadmiral.media.reconcile_and_probe")
    @patch("camadmiral.media.runtime_stream_keys", return_value=set())
    def test_missing_runtime_streams_are_reapplied(
        self,
        _runtime_keys,
        reconcile,
    ) -> None:
        repository = Mock()
        repository.managed_stream_sources.return_value = [{"stream_key": "stream_one"}]

        changed = reconcile_runtime_drift(repository)

        self.assertTrue(changed)
        reconcile.assert_called_once_with(repository)


if __name__ == "__main__":
    unittest.main()
