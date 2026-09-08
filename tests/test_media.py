import json
import subprocess
import unittest
import urllib.error
from unittest.mock import Mock, call, patch

from camadmiral.media import (
    ProbeResult,
    RelayHealthMonitor,
    RelayRuntimeActivityMonitor,
    SnapshotError,
    authenticated_rtsp_uri,
    go2rtc_websocket_url,
    probe_source,
    probe_stream,
    probe_upstreams,
    reconcile_and_probe,
    reconcile_preloads,
    reconcile_runtime_drift,
    replace_streams,
    restart_preload,
    snapshot_frame,
)


class MediaTests(unittest.TestCase):
    @patch("camadmiral.media._request")
    def test_runtime_activity_skips_go2rtc_without_managed_streams(self, request) -> None:
        repository = Mock()
        repository.managed_stream_runtime_sources.return_value = []

        monitor = RelayRuntimeActivityMonitor()

        self.assertEqual(monitor.poll(repository), set())
        request.assert_not_called()

    @patch("camadmiral.media._request")
    @patch("camadmiral.media.time.monotonic", side_effect=[100.0, 104.9, 105.0, 106.0])
    def test_runtime_activity_uses_a_time_threshold_and_advancement_clears_stall(
        self,
        _monotonic,
        request,
    ) -> None:
        runtime = {
            "stream_main": {
                "producers": [
                    {
                        "id": 7,
                        "bytes_recv": 1000,
                        "receivers": [
                            {
                                "codec": {
                                    "codec_name": "h264",
                                    "codec_type": "video",
                                },
                                "packets": 20,
                            }
                        ],
                    }
                ],
                "consumers": [{"id": 9}],
            }
        }
        request.side_effect = lambda *_args: json.dumps(runtime).encode()
        repository = Mock()
        repository.managed_stream_runtime_sources.return_value = [
            {
                "stream_uuid": "stream-1",
                "stream_key": "stream_main",
                "camera_uuid": "camera-1",
            }
        ]
        monitor = RelayRuntimeActivityMonitor(stall_threshold=5.0)

        self.assertEqual(monitor.poll(repository), set())
        self.assertEqual(monitor.poll(repository), set())
        self.assertEqual(monitor.poll(repository), {"camera-1"})
        runtime["stream_main"]["producers"][0]["receivers"][0]["packets"] = 21
        self.assertEqual(monitor.poll(repository), set())
        self.assertEqual(monitor.stalled_camera_uuids, set())
        repository.managed_stream_runtime_sources.assert_called_with()

    @patch("camadmiral.media._request")
    @patch("camadmiral.media.time.monotonic", side_effect=[100.0, 105.0, 106.0, 112.0])
    def test_runtime_activity_only_tracks_streams_with_active_consumers(
        self,
        _monotonic,
        request,
    ) -> None:
        runtime = {
            "stream_main": {
                "producers": [
                    {
                        "id": 7,
                        "bytes_recv": 1000,
                        "receivers": [
                            {
                                "codec": {
                                    "codec_name": "h264",
                                    "codec_type": "video",
                                },
                                "packets": 20,
                            }
                        ],
                    }
                ],
                "consumers": [{"id": 9}],
            }
        }
        request.side_effect = lambda *_args: json.dumps(runtime).encode()
        repository = Mock()
        repository.managed_stream_runtime_sources.return_value = [
            {
                "stream_uuid": "stream-1",
                "stream_key": "stream_main",
                "camera_uuid": "camera-1",
            }
        ]
        monitor = RelayRuntimeActivityMonitor(stall_threshold=5.0)

        self.assertEqual(monitor.poll(repository), set())
        self.assertEqual(monitor.poll(repository), {"camera-1"})
        runtime["stream_main"]["consumers"] = []
        self.assertEqual(monitor.poll(repository), set())
        runtime["stream_main"]["consumers"] = [{"id": 10}]
        self.assertEqual(monitor.poll(repository), set())

    @patch("camadmiral.media._request")
    @patch("camadmiral.media.time.monotonic", side_effect=[100.0, 105.0, 106.0])
    def test_runtime_activity_tracks_missing_and_replacement_producers(
        self,
        _monotonic,
        request,
    ) -> None:
        runtime = {
            "stream_main": {
                "producers": [
                    {
                        "id": 7,
                        "bytes_recv": 1000,
                        "receivers": [
                            {
                                "codec": {
                                    "codec_name": "h264",
                                    "codec_type": "video",
                                },
                                "packets": 20,
                            }
                        ],
                    }
                ],
                "consumers": [{"id": 9}],
            }
        }
        request.side_effect = lambda *_args: json.dumps(runtime).encode()
        repository = Mock()
        repository.managed_stream_runtime_sources.return_value = [
            {
                "stream_uuid": "stream-1",
                "stream_key": "stream_main",
                "camera_uuid": "camera-1",
            }
        ]
        monitor = RelayRuntimeActivityMonitor(stall_threshold=5.0)

        self.assertEqual(monitor.poll(repository), set())
        runtime["stream_main"]["producers"] = []
        self.assertEqual(monitor.poll(repository), {"camera-1"})
        runtime["stream_main"]["producers"] = [
            {
                "id": 8,
                "bytes_recv": 100,
                "receivers": [
                    {
                        "codec": {
                            "codec_name": "h264",
                            "codec_type": "video",
                        },
                        "packets": 1,
                    }
                ],
            }
        ]
        self.assertEqual(monitor.poll(repository), set())

    @patch("camadmiral.media.snapshot_frame", return_value=b"\xff\xd8\xffidle\xff\xd9")
    @patch("camadmiral.media._request")
    def test_relay_health_uses_active_counters_and_periodically_samples_each_camera(
        self,
        request,
        frame,
    ) -> None:
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
        request.side_effect = lambda *_args: json.dumps(runtime).encode()
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
        repository.managed_stream_sources.side_effect = [sources, [sources[1]]] * 3
        monitor = RelayHealthMonitor(frame_probe_interval=60)

        first = monitor.probe(repository)
        second = monitor.probe(repository)
        runtime["stream_active"]["producers"][0]["receivers"][0]["packets"] = 21
        third = monitor.probe(repository)

        self.assertEqual(first["active"].status, "ready")
        self.assertEqual(first["idle"].status, "ready")
        self.assertEqual(second["active"].status, "unavailable")
        self.assertNotIn("idle", second)
        self.assertEqual(third["active"].status, "ready")
        self.assertCountEqual(
            [call.args for call in frame.call_args_list],
            [("stream_active",), ("stream_idle",)],
        )
        self.assertTrue(all(call.kwargs == {"width": 320} for call in frame.call_args_list))
        self.assertEqual(monitor.cached_frame("camera-idle").content, b"\xff\xd8\xffidle\xff\xd9")
        repository.managed_stream_sources.assert_any_call(
            include_auth_failed=False,
            role_bound_only=True,
        )
        repository.managed_stream_sources.assert_any_call(
            include_auth_failed=False,
            bound_role="detect",
        )
        self.assertEqual(request.call_args_list[0].args, ("GET", "/api/streams"))

    @patch("camadmiral.media.snapshot_frame", return_value=b"\xff\xd8\xffframe\xff\xd9")
    @patch("camadmiral.media._request", return_value=b"{}")
    def test_periodic_frame_batch_rotates_fairly_across_cameras(
        self,
        _request,
        frame,
    ) -> None:
        sources = [
            {
                "stream_uuid": f"stream-{index}",
                "stream_key": f"stream_{index}",
                "camera_uuid": f"camera-{index}",
                "uri": f"rtsp://192.0.2.{index + 1}/live",
                "username": "",
                "password": "",
            }
            for index in range(3)
        ]
        repository = Mock()
        repository.managed_stream_sources.return_value = sources
        monitor = RelayHealthMonitor(
            frame_probe_interval=3600,
            frame_probe_batch_size=1,
        )

        monitor.probe(repository)
        monitor.probe(repository)
        monitor.probe(repository)

        self.assertEqual(
            [call.args[0] for call in frame.call_args_list],
            ["stream_0", "stream_1", "stream_2"],
        )

    @patch("camadmiral.media.time.monotonic", return_value=100)
    @patch(
        "camadmiral.media.snapshot_frame",
        side_effect=[SnapshotError("synthetic outage"), b"\xff\xd8\xffrecovered\xff\xd9"],
    )
    @patch("camadmiral.media._request", return_value=b"{}")
    def test_failed_periodic_frame_retries_on_next_health_cycle(
        self,
        _request,
        frame,
        _monotonic,
    ) -> None:
        source = {
            "stream_uuid": "stream-1",
            "stream_key": "stream_1",
            "camera_uuid": "camera-1",
            "uri": "rtsp://192.0.2.1/live",
            "username": "",
            "password": "",
        }
        repository = Mock()
        repository.managed_stream_sources.return_value = [source]
        monitor = RelayHealthMonitor(frame_probe_interval=60)

        first = monitor.probe(repository)
        second = monitor.probe(repository)

        self.assertEqual(first["stream-1"].status, "unavailable")
        self.assertEqual(second["stream-1"].status, "ready")
        self.assertEqual(frame.call_count, 2)

    @patch("camadmiral.media.time.monotonic", return_value=100)
    @patch(
        "camadmiral.media.snapshot_frame",
        side_effect=[
            SnapshotError("synthetic outage"),
            b"\xff\xd8\xffsecond\xff\xd9",
            b"\xff\xd8\xffthird\xff\xd9",
            b"\xff\xd8\xffrecovered\xff\xd9",
        ],
    )
    @patch("camadmiral.media._request", return_value=b"{}")
    def test_failed_retry_does_not_starve_never_sampled_cameras(
        self,
        _request,
        frame,
        _monotonic,
    ) -> None:
        sources = [
            {
                "stream_uuid": f"stream-{index}",
                "stream_key": f"stream_{index}",
                "camera_uuid": f"camera-{index}",
                "uri": f"rtsp://192.0.2.{index + 1}/live",
                "username": "",
                "password": "",
            }
            for index in range(3)
        ]
        repository = Mock()
        repository.managed_stream_sources.return_value = sources
        monitor = RelayHealthMonitor(
            frame_probe_interval=60,
            frame_probe_batch_size=1,
        )

        for _ in range(4):
            monitor.probe(repository)

        self.assertEqual(
            [call.args[0] for call in frame.call_args_list],
            ["stream_0", "stream_1", "stream_2", "stream_0"],
        )

    @patch(
        "camadmiral.media.snapshot_frame",
        side_effect=SnapshotError("synthetic camera outage"),
    )
    @patch("camadmiral.media._request", return_value=b"{}")
    def test_periodic_camera_failure_updates_all_idle_role_streams(
        self,
        _request,
        _frame,
    ) -> None:
        sources = [
            {
                "stream_uuid": "detect",
                "stream_key": "stream_detect",
                "camera_uuid": "camera-1",
                "uri": "rtsp://192.0.2.1/sub",
                "username": "",
                "password": "",
            },
            {
                "stream_uuid": "record",
                "stream_key": "stream_record",
                "camera_uuid": "camera-1",
                "uri": "rtsp://192.0.2.1/main",
                "username": "",
                "password": "",
            },
        ]
        repository = Mock()
        repository.managed_stream_sources.side_effect = [sources, [sources[0]]]
        monitor = RelayHealthMonitor()

        results = monitor.probe(repository)

        self.assertEqual(results["detect"].status, "unavailable")
        self.assertEqual(results["record"].status, "unavailable")
        recorded = repository.record_probe_results.call_args.args[0]
        self.assertEqual(recorded["detect"].status, "unavailable")
        self.assertEqual(recorded["record"].status, "unavailable")

    @patch(
        "camadmiral.media.snapshot_frame",
        side_effect=SnapshotError("synthetic substream failure"),
    )
    @patch("camadmiral.media._request")
    def test_active_stream_counter_overrides_camera_level_frame_failure(
        self,
        request,
        _frame,
    ) -> None:
        sources = [
            {
                "stream_uuid": "detect",
                "stream_key": "stream_detect",
                "camera_uuid": "camera-1",
                "uri": "rtsp://192.0.2.1/sub",
                "username": "",
                "password": "",
            },
            {
                "stream_uuid": "record",
                "stream_key": "stream_record",
                "camera_uuid": "camera-1",
                "uri": "rtsp://192.0.2.1/main",
                "username": "",
                "password": "",
            },
        ]
        runtime = {
            "stream_record": {
                "producers": [{
                    "id": 7,
                    "bytes_recv": 1000,
                    "receivers": [{
                        "codec": {"codec_name": "h264", "codec_type": "video"},
                        "packets": 20,
                    }],
                }],
                "consumers": [{"id": 9}],
            }
        }
        request.return_value = json.dumps(runtime).encode()
        repository = Mock()
        repository.managed_stream_sources.side_effect = [sources, [sources[0]]]
        monitor = RelayHealthMonitor()

        results = monitor.probe(repository)

        self.assertEqual(results["detect"].status, "unavailable")
        self.assertEqual(results["record"].status, "ready")
        recorded = repository.record_probe_results.call_args.args[0]
        self.assertEqual(recorded["detect"].status, "unavailable")
        self.assertEqual(recorded["record"].status, "ready")
        self.assertNotIn("camera-1", monitor._frame_probe_retries)

    @patch("camadmiral.media.probe_source", return_value=ProbeResult("auth_failed", 20))
    @patch("camadmiral.media._request")
    def test_relay_diagnoses_auth_only_after_sustained_preloaded_failure(
        self,
        request,
        probe_source_mock,
    ) -> None:
        sources = [
            {
                "stream_uuid": "detect",
                "stream_key": "stream_detect",
                "camera_uuid": "camera-1",
                "uri": "rtsp://192.0.2.10/sub",
                "username": "operator",
                "password": "synthetic-secret",
            },
            {
                "stream_uuid": "record",
                "stream_key": "stream_record",
                "camera_uuid": "camera-1",
                "uri": "rtsp://192.0.2.10/main",
                "username": "operator",
                "password": "synthetic-secret",
            },
        ]
        runtime = {
            source["stream_key"]: {
                "producers": [{"url": source["uri"]}],
                "consumers": [{"id": 9}],
            }
            for source in sources
        }
        request.side_effect = lambda _method, path, _query=None: json.dumps(
            {"stream_detect": {}} if path == "/api/preload" else runtime
        ).encode()
        repository = Mock()
        repository.managed_stream_sources.return_value = sources
        monitor = RelayHealthMonitor()

        monitor.probe(repository)
        monitor.probe(repository)

        probe_source_mock.assert_called_once_with(
            sources[0]["uri"],
            sources[0]["username"],
            sources[0]["password"],
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
            if query and query.get("src") == "stream_offline_http":
                raise urllib.error.HTTPError(
                    "http://127.0.0.1/api/preload",
                    500,
                    "synthetic camera unavailable",
                    {},
                    None,
                )
            if query and query.get("src") == "stream_offline_timeout":
                raise TimeoutError("synthetic camera unavailable")
            return b""

        request.side_effect = response

        reconcile_preloads([
            {"stream_key": "stream_offline_http"},
            {"stream_key": "stream_offline_timeout"},
            {"stream_key": "stream_online"},
        ])

        self.assertIn(
            ("PUT", "/api/preload", {"src": "stream_online", "video": "all"}),
            [call.args for call in request.call_args_list],
        )

    @patch("camadmiral.media._request")
    def test_failed_preload_removal_does_not_abort_health_cycle(self, request) -> None:
        def response(method, _path, _query=None):
            if method == "DELETE":
                raise urllib.error.HTTPError(
                    "http://127.0.0.1/api/preload",
                    500,
                    "synthetic broken preload",
                    {},
                    None,
                )
            return b""

        request.side_effect = response

        self.assertTrue(restart_preload("stream_synthetic"))
        self.assertEqual([call.args[0] for call in request.call_args_list], ["DELETE", "PUT"])

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

    @patch("camadmiral.media.wait_for_go2rtc")
    @patch("camadmiral.media._request")
    @patch("camadmiral.media.time.sleep")
    def test_replace_streams_persists_sources_then_restarts_stock_go2rtc(
        self,
        _sleep,
        request,
        wait_for_go2rtc_mock,
    ) -> None:
        sources = [
            {
                "stream_key": "stream_main",
                "uri": "rtsp://192.0.2.20:554/main?channel=1",
                "username": "camera user",
                "password": "synthetic-secret",
            },
            {
                "stream_key": "stream_sub",
                "uri": "rtsp://192.0.2.20:554/sub",
                "username": "",
                "password": "",
            },
        ]

        replace_streams(sources)

        self.assertEqual(
            wait_for_go2rtc_mock.call_args_list,
            [call(), call(attempts=50, delay=0.1)],
        )
        main_uri = "rtsp://camera%20user:synthetic-secret@192.0.2.20:554/main?channel=1"
        sub_uri = "rtsp://192.0.2.20:554/sub"
        self.assertEqual(request.call_args_list[0].args, ("PATCH", "/api/config"))
        self.assertEqual(
            json.loads(request.call_args_list[0].kwargs["body"]),
            {"streams": {"stream_main": [main_uri], "stream_sub": [sub_uri]}},
        )
        self.assertEqual(request.call_args_list[1], call("POST", "/api/restart"))

    @patch("camadmiral.media.wait_for_go2rtc")
    @patch(
        "camadmiral.media._request",
        side_effect=RuntimeError("synthetic persistence failure"),
    )
    def test_replace_streams_does_not_restart_after_persistence_failure(
        self,
        request,
        _wait_for_go2rtc,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "synthetic persistence failure"):
            replace_streams([
                {
                    "stream_key": "stream_main",
                    "uri": "rtsp://192.0.2.20/main",
                    "username": "",
                    "password": "",
                },
                {
                    "stream_key": "stream_sub",
                    "uri": "rtsp://192.0.2.20/sub",
                    "username": "",
                    "password": "",
                },
            ])

        request.assert_called_once()

    @patch("camadmiral.media.wait_for_go2rtc")
    @patch(
        "camadmiral.media._request",
        side_effect=[b"", RuntimeError("synthetic restart failure")],
    )
    @patch("camadmiral.media.time.sleep")
    def test_replace_streams_propagates_unexpected_restart_failure(
        self,
        _sleep,
        request,
        _wait_for_go2rtc,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "synthetic restart failure"):
            replace_streams([
                {
                    "stream_key": "stream_main",
                    "uri": "rtsp://192.0.2.20/main",
                    "username": "",
                    "password": "",
                }
            ])

        self.assertEqual(request.call_count, 2)

    @patch("camadmiral.media.time.sleep")
    @patch("camadmiral.media.wait_for_go2rtc")
    @patch(
        "camadmiral.media._request",
        side_effect=[b"", urllib.error.URLError("synthetic restart disconnect")],
    )
    def test_replace_streams_accepts_expected_restart_disconnect(
        self,
        request,
        wait_for_go2rtc_mock,
        _sleep,
    ) -> None:
        replace_streams([
            {
                "stream_key": "stream_main",
                "uri": "rtsp://192.0.2.20/main",
                "username": "",
                "password": "",
            }
        ])

        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            wait_for_go2rtc_mock.call_args_list,
            [call(), call(attempts=50, delay=0.1)],
        )

    @patch("camadmiral.media.wait_for_go2rtc")
    @patch("camadmiral.media._request")
    def test_replace_streams_skips_restart_for_empty_batch(
        self,
        request,
        wait_for_go2rtc_mock,
    ) -> None:
        replace_streams([])

        wait_for_go2rtc_mock.assert_called_once_with()
        request.assert_not_called()

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
        reconcile_preloads_mock.assert_called_once_with([])
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
