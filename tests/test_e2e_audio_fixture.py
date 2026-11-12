"""The empty-audio fixture must never mute the synthetic video track."""
import unittest

from e2e.backchannel_proxy import is_microphone_packet


def rtp(channel, payload_type, marker=False):
    payload = bytes((0x80, payload_type | (0x80 if marker else 0))) + bytes(10)
    return b"$" + bytes((channel,)) + len(payload).to_bytes(2, "big") + payload


class EmptyAudioFixtureTests(unittest.TestCase):
    def test_both_track_orders_mute_only_microphone(self):
        for audio_channel, video_channel in ((0, 2), (2, 0)):
            for marker in (False, True):
                with self.subTest(audio=audio_channel, marker=marker):
                    self.assertTrue(is_microphone_packet(rtp(audio_channel, 0, marker)))
                with self.subTest(video=video_channel, marker=marker):
                    self.assertFalse(is_microphone_packet(rtp(video_channel, 96, marker)))

    def test_control_packets_and_partial_frames_are_not_microphone_samples(self):
        self.assertFalse(is_microphone_packet(b"$\x01\x00\x0c\x80\xc8" + bytes(10)))
        self.assertFalse(is_microphone_packet(b"$\x00"))
        self.assertFalse(is_microphone_packet(b"RTSP/1.0 200 OK\r\n"))


if __name__ == "__main__":
    unittest.main()
