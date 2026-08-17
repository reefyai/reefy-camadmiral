from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape

SOAP_NAMESPACE = "http://www.w3.org/2003/05/soap-envelope"
DEVICE_NAMESPACE = "http://www.onvif.org/ver10/device/wsdl"
MEDIA_NAMESPACE = "http://www.onvif.org/ver10/media/wsdl"
SCHEMA_NAMESPACE = "http://www.onvif.org/ver10/schema"
MAX_PROFILES = 16


@dataclass
class OnvifInspectionError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(element: ET.Element, name: str) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _parse_xml(payload: bytes) -> ET.Element:
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise OnvifInspectionError("invalid_response", "Camera returned invalid ONVIF XML") from exc


def _to_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _to_float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0


def _is_auth_fault(status: int, payload: bytes) -> bool:
    if status in {401, 403}:
        return True
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return False
    fault_text = " ".join(
        child.text or ""
        for child in root.iter()
        if _local_name(child.tag) in {"Value", "Text", "Reason", "Subcode"}
    ).lower()
    return any(
        marker in fault_text
        for marker in {"notauthorized", "not authorized", "unauthorized", "authenticate", "credentials"}
    )


def _ws_security_header(username: str, password: str) -> str:
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()
    ).decode("ascii")
    nonce_value = base64.b64encode(nonce).decode("ascii")
    return (
        '<soap:Header><wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-utility-1.0.xsd"><wsse:UsernameToken>'
        f"<wsse:Username>{escape(username)}</wsse:Username>"
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
        '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_value}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security></soap:Header>"
    )


def _soap_envelope(body: str, username: str | None, password: str | None) -> bytes:
    header = _ws_security_header(username, password or "") if username is not None else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP_NAMESPACE}"'
        f' xmlns:tds="{DEVICE_NAMESPACE}" xmlns:trt="{MEDIA_NAMESPACE}"'
        f' xmlns:tt="{SCHEMA_NAMESPACE}">{header}<soap:Body>{body}</soap:Body></soap:Envelope>'
    ).encode("utf-8")


def _soap_request(url: str, action: str, payload: bytes) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
            "SOAPAction": f'"{action}"',
            "User-Agent": "CamAdmiral/0.1",
        },
    )


def _send_request(
    request: urllib.request.Request,
    timeout: float,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, bytes, Any]:
    try:
        open_request = opener.open if opener is not None else urllib.request.urlopen
        with open_request(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise OnvifInspectionError("unreachable", "Camera ONVIF service is unreachable") from exc


def _http_digest_challenge(status: int, headers: Any) -> bool:
    if status != 401 or headers is None:
        return False
    challenge = str(headers.get("WWW-Authenticate", "")).strip().lower()
    return challenge.startswith("digest ")


def _http_digest_opener(url: str, username: str, password: str) -> urllib.request.OpenerDirector:
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, url, username, password)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(password_manager))


def _soap_post(
    url: str,
    action: str,
    body: str,
    timeout: float,
    username: str | None = None,
    password: str | None = None,
) -> ET.Element:
    request = _soap_request(url, action, _soap_envelope(body, username, password))
    status, payload, headers = _send_request(request, timeout)
    digest_attempted = False
    if (
        username is not None
        and status in {200, 400}
        and _is_auth_fault(status, payload)
        and not _http_digest_challenge(status, headers)
    ):
        challenge_request = _soap_request(url, action, _soap_envelope(body, None, None))
        challenge_status, challenge_payload, challenge_headers = _send_request(
            challenge_request,
            timeout,
        )
        if _http_digest_challenge(challenge_status, challenge_headers):
            digest_request = _soap_request(url, action, _soap_envelope(body, None, None))
            digest_opener = _http_digest_opener(url, username, password or "")
            status, payload, headers = _send_request(digest_request, timeout, digest_opener)
            digest_attempted = True
        else:
            status, payload, headers = challenge_status, challenge_payload, challenge_headers
    if username is not None and not digest_attempted and _http_digest_challenge(status, headers):
        digest_request = _soap_request(url, action, _soap_envelope(body, None, None))
        digest_opener = _http_digest_opener(url, username, password or "")
        status, payload, headers = _send_request(digest_request, timeout, digest_opener)
    if _is_auth_fault(status, payload):
        raise OnvifInspectionError("credentials_required", "Credentials required")
    if status != 200:
        raise OnvifInspectionError("onvif_error", f"Camera returned ONVIF HTTP {status}")
    root = _parse_xml(payload)
    if any(_local_name(element.tag) == "Fault" for element in root.iter()):
        raise OnvifInspectionError("onvif_error", "Camera rejected the ONVIF request")
    return root


def _safe_candidate_url(candidate: dict[str, Any]) -> str:
    candidate_ip = ipaddress.ip_address(str(candidate.get("ip") or ""))
    if not candidate_ip.is_private:
        raise OnvifInspectionError("unsafe_endpoint", "Camera endpoint is outside the private LAN")
    onvif = candidate.get("onvif") or {}
    for value in onvif.get("service_urls", []):
        parsed = urllib.parse.urlsplit(str(value))
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            continue
        try:
            endpoint_ip = ipaddress.ip_address(parsed.hostname or "")
        except ValueError:
            continue
        if endpoint_ip == candidate_ip:
            return urllib.parse.urlunsplit(parsed._replace(fragment=""))
    raise OnvifInspectionError("unsafe_endpoint", "No safe ONVIF device endpoint was discovered")


def _media_service_urls(root: ET.Element, device_url: str) -> list[str]:
    urls: list[str] = []
    for service in root.iter():
        if _local_name(service.tag) != "Service":
            continue
        namespace = _first_text(service, "Namespace") or ""
        xaddr = _first_text(service, "XAddr") or ""
        if namespace.rstrip("/") == MEDIA_NAMESPACE.rstrip("/") and xaddr:
            urls.append(xaddr)
    parsed = urllib.parse.urlsplit(device_url)
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    for path in ("/onvif/media_service", "/onvif/Media", "/onvif/media"):
        value = f"{base}{path}"
        if value not in urls:
            urls.append(value)
    return urls


def _safe_service_url(value: str, candidate_ip: str) -> str | None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return None
    try:
        endpoint_ip = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        return None
    if endpoint_ip != ipaddress.ip_address(candidate_ip):
        return None
    return urllib.parse.urlunsplit(parsed._replace(fragment=""))


def _profiles(root: ET.Element) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for element in root.iter():
        if _local_name(element.tag) != "Profiles":
            continue
        token = element.attrib.get("token") or element.attrib.get("Token")
        if not token:
            continue
        encoder = next(
            (
                child
                for child in element.iter()
                if _local_name(child.tag) == "VideoEncoderConfiguration"
            ),
            None,
        )
        profiles.append(
            {
                "token": token,
                "name": _first_text(element, "Name") or token,
                "width": _to_int(_first_text(encoder, "Width")) if encoder is not None else 0,
                "height": _to_int(_first_text(encoder, "Height")) if encoder is not None else 0,
                "encoding": _first_text(encoder, "Encoding") if encoder is not None else None,
                "fps": _to_float(_first_text(encoder, "FrameRateLimit")) if encoder is not None else 0,
                "bitrate_kbps": _to_int(_first_text(encoder, "BitrateLimit")) if encoder is not None else 0,
                "uri": None,
            }
        )
        if len(profiles) >= MAX_PROFILES:
            break
    return profiles


def _safe_rtsp_uri(value: str, candidate_ip: str) -> str | None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"rtsp", "rtsps"}:
        return None
    host = parsed.hostname or ""
    if host == "0.0.0.0":
        host = candidate_ip
    try:
        stream_ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if stream_ip != ipaddress.ip_address(candidate_ip):
        return None
    display_host = f"[{host}]" if ":" in host else host
    try:
        port = parsed.port
    except ValueError:
        return None
    if port:
        display_host = f"{display_host}:{port}"
    return urllib.parse.urlunsplit(parsed._replace(netloc=display_host, fragment=""))


def inspect_onvif_candidate(
    candidate: dict[str, Any],
    timeout: float = 4.0,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    if not candidate.get("onvif"):
        raise OnvifInspectionError("not_onvif", "This candidate did not advertise ONVIF")
    candidate_ip = str(candidate.get("ip") or "")
    device_url = _safe_candidate_url(candidate)
    services = _soap_post(
        device_url,
        f"{DEVICE_NAMESPACE}/GetServices",
        "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>",
        timeout,
        username,
        password,
    )

    media_urls = [
        safe
        for value in _media_service_urls(services, device_url)
        if (safe := _safe_service_url(value, candidate_ip))
    ]
    profiles: list[dict[str, Any]] = []
    used_media_url = None
    last_error: OnvifInspectionError | None = None
    for media_url in media_urls:
        try:
            response = _soap_post(
                media_url,
                f"{MEDIA_NAMESPACE}/GetProfiles",
                "<trt:GetProfiles/>",
                timeout,
                username,
                password,
            )
        except OnvifInspectionError as exc:
            if exc.code == "credentials_required":
                raise
            last_error = exc
            continue
        profiles = _profiles(response)
        if profiles:
            used_media_url = media_url
            break
    if not profiles or not used_media_url:
        if last_error:
            raise last_error
        raise OnvifInspectionError("no_profiles", "Camera did not report ONVIF media profiles")

    for profile in profiles:
        body = (
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            "</trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(str(profile['token']))}</trt:ProfileToken>"
            "</trt:GetStreamUri>"
        )
        response = _soap_post(
            used_media_url,
            f"{MEDIA_NAMESPACE}/GetStreamUri",
            body,
            timeout,
            username,
            password,
        )
        uri = _first_text(response, "Uri")
        profile["uri"] = _safe_rtsp_uri(uri, candidate_ip) if uri else None

    return {
        "status": "ok",
        "candidate_uuid": candidate.get("candidate_uuid"),
        "device_service": device_url,
        "media_service": used_media_url,
        "profiles": profiles,
    }
