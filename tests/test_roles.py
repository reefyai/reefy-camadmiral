import unittest

from camadmiral.roles import select_stream_roles


class StreamRoleTests(unittest.TestCase):
    def test_selects_highest_quality_for_record_and_lowest_suitable_for_detect(self) -> None:
        profiles = [
            {"token": "high", "uri": "rtsp://192.168.1.2/high", "width": 2560, "height": 1440, "encoding": "H265", "fps": 20},
            {"token": "medium", "uri": "rtsp://192.168.1.2/medium", "width": 1280, "height": 720, "encoding": "H264", "fps": 15},
            {"token": "low", "uri": "rtsp://192.168.1.2/low", "width": 640, "height": 360, "encoding": "H264", "fps": 10},
        ]

        self.assertEqual(select_stream_roles(profiles), {"record": "high", "detect": "low"})

    def test_one_usable_stream_gets_both_roles(self) -> None:
        profile = {"token": "only", "uri": "rtsp://192.168.1.2/only", "width": 640, "height": 360, "encoding": "H264"}

        self.assertEqual(select_stream_roles([profile]), {"record": "only", "detect": "only"})

    def test_ignores_profiles_without_stream_uri(self) -> None:
        self.assertEqual(select_stream_roles([{"token": "missing", "width": 1920, "height": 1080}]), {})


if __name__ == "__main__":
    unittest.main()
