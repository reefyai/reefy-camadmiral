import io
import http.server
import threading
import urllib.error
import unittest
from unittest.mock import patch

from camadmiral import onvif_client


SERVICES = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
 <s:Body><tds:GetServicesResponse><tds:Service>
  <tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace>
  <tds:XAddr>http://192.168.10.20:2020/onvif/Media</tds:XAddr>
 </tds:Service></tds:GetServicesResponse></s:Body>
</s:Envelope>"""

PROFILES = b"""<?xml version="1.0"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
 xmlns:tt="http://www.onvif.org/ver10/schema">
 <env:Body><trt:GetProfilesResponse>
  <trt:Profiles token="profile_main"><tt:Name>Main stream</tt:Name>
   <tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding>
    <tt:Resolution><tt:Width>2560</tt:Width><tt:Height>1440</tt:Height></tt:Resolution>
    <tt:RateControl><tt:FrameRateLimit>20</tt:FrameRateLimit><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>
   </tt:VideoEncoderConfiguration>
  </trt:Profiles>
  <trt:Profiles token="profile_sub"><tt:Name>Sub stream</tt:Name>
   <tt:VideoEncoderConfiguration><tt:Encoding>H264</tt:Encoding>
    <tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>
   </tt:VideoEncoderConfiguration>
  </trt:Profiles>
 </trt:GetProfilesResponse></env:Body>
</env:Envelope>"""

AUTH_FAULT = b"""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
<s:Body><s:Fault><s:Code><s:Subcode><s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>
<s:Reason><s:Text>Sender not authorized</s:Text></s:Reason></s:Fault></s:Body></s:Envelope>"""


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200, headers=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.payload


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class DigestChallengeHandler(http.server.BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        authorization = self.headers.get("Authorization")
        type(self).requests.append((payload, authorization))
        if not authorization:
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate",
                'Digest realm="synthetic-camera", nonce="synthetic-nonce", algorithm=MD5, qop="auth"',
            )
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(SERVICES)))
        self.end_headers()
        self.wfile.write(SERVICES)

    def log_message(self, _format, *_args):
        return


def candidate() -> dict:
    return {
        "candidate_uuid": "candidate-1",
        "ip": "192.168.10.20",
        "onvif": {
            "service_urls": [
                "http://[fe80::1]/onvif/device_service",
                "http://192.168.10.20:2020/onvif/device_service",
            ]
        },
    }


class OnvifClientTests(unittest.TestCase):
    def test_real_http_digest_handler_negotiates_challenge(self) -> None:
        DigestChallengeHandler.requests = []
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DigestChallengeHandler)
        except PermissionError:
            self.skipTest("loopback listeners are unavailable in this sandbox")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = onvif_client._soap_post(
                f"http://127.0.0.1:{server.server_port}/onvif/media_service",
                "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
                "<trt:GetProfiles/>",
                4,
                "operator",
                "synthetic-secret",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result.tag.rsplit("}", 1)[-1], "Envelope")
        self.assertEqual(len(DigestChallengeHandler.requests), 3)
        initial, digest_challenge, digest_authenticated = DigestChallengeHandler.requests
        self.assertIn(b"PasswordDigest", initial[0])
        self.assertIsNone(initial[1])
        self.assertNotIn(b"Security", digest_challenge[0])
        self.assertIsNone(digest_challenge[1])
        self.assertNotIn(b"Security", digest_authenticated[0])
        self.assertTrue(digest_authenticated[1].startswith("Digest "))

    def test_http_digest_challenge_retries_without_ws_security(self) -> None:
        challenge = {"WWW-Authenticate": 'Digest realm="camera", nonce="synthetic", qop="auth"'}
        initial_payloads = []

        def challenge_request(request, timeout):
            initial_payloads.append(request.data)
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                challenge,
                io.BytesIO(b""),
            )

        digest_opener = FakeOpener(response=FakeResponse(SERVICES))
        with patch.object(onvif_client.urllib.request, "urlopen", side_effect=challenge_request), patch.object(
            onvif_client,
            "_http_digest_opener",
            return_value=digest_opener,
        ) as create_digest:
            result = onvif_client._soap_post(
                "http://192.168.10.20/onvif/media_service",
                "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
                "<trt:GetProfiles/>",
                4,
                "operator",
                "synthetic-secret",
            )

        self.assertEqual(result.tag.rsplit("}", 1)[-1], "Envelope")
        self.assertIn(b"PasswordDigest", initial_payloads[0])
        self.assertEqual(len(digest_opener.requests), 1)
        self.assertNotIn(b"Security", digest_opener.requests[0].data)
        self.assertNotIn(b"operator", digest_opener.requests[0].data)
        create_digest.assert_called_once_with(
            "http://192.168.10.20/onvif/media_service",
            "operator",
            "synthetic-secret",
        )

    def test_http_400_auth_fault_probes_for_digest_challenge(self) -> None:
        challenge = {"WWW-Authenticate": 'Digest realm="camera", nonce="synthetic", qop="auth"'}
        digest_challenge = urllib.error.HTTPError(
            "http://192.168.10.20/onvif/media_service",
            401,
            "Unauthorized",
            challenge,
            io.BytesIO(b""),
        )
        digest_opener = FakeOpener(response=FakeResponse(SERVICES))
        with patch.object(
            onvif_client.urllib.request,
            "urlopen",
            side_effect=[FakeResponse(AUTH_FAULT, status=400), digest_challenge],
        ) as send, patch.object(
            onvif_client,
            "_http_digest_opener",
            return_value=digest_opener,
        ) as create_digest:
            result = onvif_client._soap_post(
                "http://192.168.10.20/onvif/media_service",
                "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
                "<trt:GetProfiles/>",
                4,
                "operator",
                "synthetic-secret",
            )

        self.assertEqual(result.tag.rsplit("}", 1)[-1], "Envelope")
        self.assertEqual(send.call_count, 2)
        initial_request = send.call_args_list[0].args[0]
        challenge_request = send.call_args_list[1].args[0]
        self.assertIn(b"PasswordDigest", initial_request.data)
        self.assertNotIn(b"Security", challenge_request.data)
        self.assertEqual(len(digest_opener.requests), 1)
        self.assertNotIn(b"Security", digest_opener.requests[0].data)
        create_digest.assert_called_once_with(
            "http://192.168.10.20/onvif/media_service",
            "operator",
            "synthetic-secret",
        )

    def test_rejected_http_digest_credentials_report_credentials_required(self) -> None:
        challenge = {"WWW-Authenticate": 'Digest realm="camera", nonce="synthetic", qop="auth"'}
        initial_error = urllib.error.HTTPError(
            "http://192.168.10.20/onvif/media_service",
            401,
            "Unauthorized",
            challenge,
            io.BytesIO(b""),
        )
        digest_error = urllib.error.HTTPError(
            "http://192.168.10.20/onvif/media_service",
            401,
            "Unauthorized",
            challenge,
            io.BytesIO(AUTH_FAULT),
        )
        digest_opener = FakeOpener(error=digest_error)

        with patch.object(onvif_client.urllib.request, "urlopen", side_effect=initial_error), patch.object(
            onvif_client,
            "_http_digest_opener",
            return_value=digest_opener,
        ):
            with self.assertRaises(onvif_client.OnvifInspectionError) as raised:
                onvif_client._soap_post(
                    "http://192.168.10.20/onvif/media_service",
                    "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
                    "<trt:GetProfiles/>",
                    4,
                    "operator",
                    "incorrect-secret",
                )

        self.assertEqual(raised.exception.code, "credentials_required")
        self.assertEqual(len(digest_opener.requests), 1)

    def test_authenticated_request_uses_password_digest_without_plaintext_password(self) -> None:
        captured = []

        def reject(request, timeout):
            captured.append(request.data)
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(AUTH_FAULT))

        with patch.object(onvif_client.urllib.request, "urlopen", side_effect=reject):
            with self.assertRaises(onvif_client.OnvifInspectionError):
                onvif_client.inspect_onvif_candidate(candidate(), username="operator", password="synthetic-secret")

        self.assertEqual(len(captured), 1)
        self.assertIn(b"PasswordDigest", captured[0])
        self.assertIn(b"operator", captured[0])
        self.assertNotIn(b"synthetic-secret", captured[0])

    def test_inspection_parses_profiles_and_sanitizes_stream_uris(self) -> None:
        requests = []

        def respond(request, timeout):
            requests.append(request)
            body = request.data.decode("utf-8")
            if "GetServices" in body:
                return FakeResponse(SERVICES)
            if "GetProfiles" in body:
                return FakeResponse(PROFILES)
            if "profile_main" in body:
                payload = b"<s:Envelope xmlns:s='http://www.w3.org/2003/05/soap-envelope'><s:Body><Uri>rtsp://user:secret@192.168.10.20:554/main?x=1&amp;y=2</Uri></s:Body></s:Envelope>"
                return FakeResponse(payload)
            payload = b"<s:Envelope xmlns:s='http://www.w3.org/2003/05/soap-envelope'><s:Body><Uri>rtsp://0.0.0.0:554/sub</Uri></s:Body></s:Envelope>"
            return FakeResponse(payload)

        with patch.object(onvif_client.urllib.request, "urlopen", side_effect=respond):
            result = onvif_client.inspect_onvif_candidate(candidate())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["profiles"]), 2)
        main, sub = result["profiles"]
        self.assertEqual((main["width"], main["height"], main["fps"]), (2560, 1440, 20.0))
        self.assertEqual(main["encoding"], "H264")
        self.assertEqual(main["uri"], "rtsp://192.168.10.20:554/main?x=1&y=2")
        self.assertEqual(sub["uri"], "rtsp://192.168.10.20:554/sub")
        self.assertNotIn("secret", str(result))
        self.assertEqual(len(requests), 4)

    def test_auth_rejection_stops_after_first_request(self) -> None:
        calls = []

        def reject(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(AUTH_FAULT),
            )

        with patch.object(onvif_client.urllib.request, "urlopen", side_effect=reject):
            with self.assertRaises(onvif_client.OnvifInspectionError) as raised:
                onvif_client.inspect_onvif_candidate(candidate())

        self.assertEqual(raised.exception.code, "credentials_required")
        self.assertEqual(len(calls), 1)

    def test_external_device_service_is_rejected(self) -> None:
        unsafe = candidate()
        unsafe["onvif"]["service_urls"] = ["http://203.0.113.10/onvif/device_service"]

        with self.assertRaises(onvif_client.OnvifInspectionError) as raised:
            onvif_client.inspect_onvif_candidate(unsafe)

        self.assertEqual(raised.exception.code, "unsafe_endpoint")

    def test_non_onvif_candidate_is_rejected_without_network_request(self) -> None:
        with patch.object(onvif_client.urllib.request, "urlopen") as request:
            with self.assertRaises(onvif_client.OnvifInspectionError) as raised:
                onvif_client.inspect_onvif_candidate({"ip": "192.168.10.20", "onvif": None})

        self.assertEqual(raised.exception.code, "not_onvif")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
