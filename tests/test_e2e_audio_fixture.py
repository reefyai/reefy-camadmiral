"""The empty-audio fixture follows SDP and negotiated transport channels."""
import asyncio
import unittest
from unittest.mock import patch

from e2e import backchannel_proxy

from e2e.backchannel_proxy import audio_track_controls, audio_setup_channels


def describe(audio_first, payload, absolute=False):
    def media(kind, track, codec, pt):
        control = ("rtsp://synthetic.invalid/main/" if absolute else "") + track
        return f"m={kind} 0 RTP/AVP {pt}\r\na=rtpmap:{pt} {codec}\r\na=control:{control}\r\n"
    audio = media("audio", "trackID=0" if audio_first else "trackID=1", "PCMU/8000", payload)
    video = media("video", "trackID=1" if audio_first else "trackID=0", "H264/90000", 96)
    return ("RTSP/1.0 200 OK\r\n\r\nv=0\r\n" + (audio + video if audio_first else video + audio)).encode()


def setup(channels, status=200):
    return f"RTSP/1.0 {status} OK\r\nTransport: RTP/AVP/TCP;unicast;interleaved={channels[0]}-{channels[1]}\r\n\r\n".encode()


class EmptyAudioFixtureTests(unittest.TestCase):
    def test_track_order_payload_numbers_and_transport_allocation_are_independent(self):
        for first in (False, True):
            for payload in (0, 97):
                for channels in ((0, 1), (2, 3), (6, 7)):
                    for absolute in (False, True):
                        with self.subTest(first=first, payload=payload, channels=channels, absolute=absolute):
                            controls = audio_track_controls(describe(first, payload, absolute))
                            audio_track, video_track = ((0, 1) if first else (1, 0))
                            self.assertEqual(controls, {f"trackID={audio_track}"})
                            self.assertEqual(audio_setup_channels(f"rtsp://relay.invalid/healthy/trackID={audio_track}", setup(channels), controls), set(channels))
                            self.assertEqual(audio_setup_channels(f"rtsp://relay.invalid/healthy/trackID={video_track}", setup(channels), controls), set())

    def test_failed_or_noninterleaved_setup_never_marks_channels_as_audio(self):
        controls = {"trackID=1"}
        uri = "rtsp://synthetic.invalid/healthy/trackID=1"
        self.assertEqual(audio_setup_channels(uri, setup((0, 1), 461), controls), set())
        self.assertEqual(audio_setup_channels(uri, b"RTSP/1.0 200 OK\r\nTransport: RTP/AVP;unicast;server_port=10000-10001\r\n\r\n", controls), set())
        self.assertEqual(audio_track_controls(b"RTSP/1.0 404 Not Found\r\n\r\n"), set())
        self.assertEqual(audio_track_controls(b"RTSP/1.0 200 OK\r\n\r\nv=0\r\nm=video 0 RTP/AVP 96\r\na=control:trackID=0\r\n"), set())


class NegotiatedRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_audio_mutes_negotiated_audio_and_forwards_video(self):
        class Writer:
            def __init__(self): self.data = bytearray()
            def write(self, data): self.data.extend(data)
            async def drain(self): pass
            def close(self): pass
            async def wait_closed(self): pass

        for audio_first in (False, True):
            with self.subTest(audio_first=audio_first):
                audio_track, video_track = ((0, 1) if audio_first else (1, 0))
                audio_pair, video_pair = ((0, 1), (2, 3)) if audio_first else ((2, 3), (0, 1))
                reader, source = asyncio.StreamReader(), asyncio.StreamReader()
                writer, upstream = Writer(), Writer()
                for index, (method, suffix) in enumerate((("DESCRIBE", ""), ("SETUP", f"/trackID={audio_track}"), ("SETUP", f"/trackID={video_track}"), ("PLAY", ""))):
                    reader.feed_data(f"{method} rtsp://proxy.invalid/healthy{suffix} RTSP/1.0\r\nCSeq: {index}\r\n\r\n".encode())
                reader.feed_eof()
                sdp = describe(audio_first, 97).split(b"\r\n\r\n", 1)[1]
                source.feed_data(b"RTSP/1.0 200 OK\r\nContent-Length: " + str(len(sdp)).encode() + b"\r\n\r\n" + sdp)
                source.feed_data(setup(audio_pair) + setup(video_pair) + b"RTSP/1.0 200 OK\r\n\r\n")
                def packet(channel, payload):
                    return b"$" + bytes((channel,)) + len(payload).to_bytes(2, "big") + payload
                audio = packet(audio_pair[0], b"\x80\x61microphone")
                video = packet(video_pair[0], b"\x80\x60video")
                rtcp = packet(audio_pair[1], b"\x80\xc8audio-report")
                source.feed_data(audio + video + rtcp)
                source.feed_eof()
                async def connect(*args): return source, upstream
                with patch.object(backchannel_proxy.asyncio, "open_connection", connect), patch.object(backchannel_proxy, "empty_audio", True), patch.object(backchannel_proxy, "stalled", False):
                    await backchannel_proxy.relay(reader, writer)
                self.assertIn(video, writer.data)
                self.assertNotIn(audio, writer.data)
                self.assertNotIn(rtcp, writer.data)


if __name__ == "__main__":
    unittest.main()
