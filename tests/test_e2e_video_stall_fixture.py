"""Video-only faults follow SDP rather than assuming fixed payload types."""
import asyncio
import unittest
from unittest.mock import patch
from e2e import stalled_rtsp_proxy as proxy


class VideoStallFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_freeze_video_preserves_live_audio_for_both_payload_orders(self):
        class Writer:
            def __init__(self): self.data = bytearray()
            def write(self, data): self.data.extend(data)
            async def drain(self): pass
            def close(self): pass

        for video_pt, audio_pt in ((96, 97), (97, 96), (102, 0)):
            with self.subTest(video=video_pt, audio=audio_pt):
                reader, source = asyncio.StreamReader(), asyncio.StreamReader()
                writer, upstream = Writer(), Writer()
                reader.feed_data(b'DESCRIBE rtsp://synthetic.invalid/main RTSP/1.0\r\nCSeq: 1\r\n\r\n')
                sdp = (f'v=0\r\nm=audio 0 RTP/AVP {audio_pt}\r\na=rtpmap:{audio_pt} PCMU/8000\r\n'
                       f'm=video 0 RTP/AVP {video_pt}\r\na=rtpmap:{video_pt} H264/90000\r\n').encode()
                source.feed_data(b'RTSP/1.0 200 OK\r\nContent-Type: application/sdp\r\nContent-Length: '
                                 + str(len(sdp)).encode() + b'\r\n\r\n' + sdp)
                def packet(channel, pt, text):
                    payload = bytes((128, pt | 128)) + b'0123456789' + text
                    return b'$' + bytes((channel,)) + len(payload).to_bytes(2, 'big') + payload
                video = packet(2, video_pt, b'synthetic-video')
                audio = packet(0, audio_pt, b'synthetic-audio')
                source.feed_data(video + audio)
                source.feed_eof()
                async def connect(*args): return source, upstream
                with patch.object(proxy.asyncio, 'open_connection', connect), \
                     patch.dict(proxy.os.environ, {'FREEZE_ALL_VIDEO': '1'}), \
                     patch.object(proxy, 'stalled', False), \
                     patch.object(proxy, 'frozen_video_packets', 0), \
                     patch.object(proxy, 'forwarded_audio_packets', 0):
                    await proxy.relay(reader, writer)
                    self.assertEqual(1, proxy.frozen_video_packets)
                    self.assertEqual(1, proxy.forwarded_audio_packets)
                self.assertNotIn(video, writer.data)
                self.assertIn(audio, writer.data)
