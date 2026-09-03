from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from frame_fingerprint import fingerprint_distance
from launcher import run_launcher


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "e2e-artifacts"
TRANSCRIPT = ARTIFACT_DIR / "e2e-transcript.log"
COMPOSE_FILE = ROOT / "e2e" / "compose.yaml"
COMPOSE = [
    "docker",
    "compose",
    "--project-name",
    "camadmiral-e2e",
    "--file",
    str(COMPOSE_FILE),
    "--profile",
    "moved",
    "--profile",
    "rotated",
    "--profile",
    "identity",
]


def run(*arguments: str, capture: bool = False) -> str:
    command = [*COMPOSE, *arguments]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    with TRANSCRIPT.open("a", encoding="utf-8") as transcript:
        transcript.write(f"$ {shlex.join(command)}\n")
        if completed.stdout:
            transcript.write(completed.stdout)
        if completed.stderr:
            transcript.write(completed.stderr)
        transcript.write(f"[exit {completed.returncode}]\n")
    if completed.returncode:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed.stdout.strip() if completed.stdout else ""


def scenario(name: str) -> None:
    run("run", "--rm", "test-driver", name)


def identity_consumer() -> dict[str, object]:
    payload = run(
        "exec",
        "-T",
        "camadmiral",
        "python",
        "/e2e/faults.py",
        "identity-consumer",
        capture=True,
    )
    try:
        consumer = json.loads(payload.splitlines()[-1])
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid go2rtc identity consumer response: {payload}") from exc
    if not isinstance(consumer, dict):
        raise RuntimeError(f"Invalid go2rtc identity consumer response: {payload}")
    consumer["decoder"] = identity_decoder_status(
        "identity-consumer",
        "/state/identity-consumer-status.json",
    )
    return consumer


def identity_control_consumer() -> dict[str, object]:
    payload = run(
        "exec",
        "-T",
        "camadmiral",
        "python",
        "/e2e/faults.py",
        "identity-control-consumer",
        capture=True,
    )
    try:
        consumer = json.loads(payload.splitlines()[-1])
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid control consumer response: {payload}") from exc
    if not isinstance(consumer, dict):
        raise RuntimeError(f"Invalid control consumer response: {payload}")
    consumer["decoder"] = identity_decoder_status(
        "identity-control-consumer",
        "/state/identity-control-status.json",
    )
    return consumer


def identity_decoder_status(service: str, path: str) -> dict[str, object]:
    status_payload = run(
        "exec",
        "-T",
        service,
        "cat",
        path,
        capture=True,
    )
    try:
        decoder = json.loads(status_payload.splitlines()[-1])
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid identity consumer status: {status_payload}") from exc
    if not isinstance(decoder, dict):
        raise RuntimeError(f"Invalid identity consumer status: {status_payload}")
    return decoder


def frigate_identity_consumers() -> dict[str, object]:
    payload = run(
        "exec",
        "-T",
        "camadmiral",
        "python",
        "/e2e/faults.py",
        "frigate-identity-consumers",
        capture=True,
    )
    try:
        state = json.loads(payload.splitlines()[-1])
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid Frigate consumer response: {payload}") from exc
    if not isinstance(state, dict):
        raise RuntimeError(f"Invalid Frigate consumer response: {payload}")
    return state


def wait_for_frigate_identity_consumers(
    *,
    previous: dict[str, object] | None = None,
    timeout: float = 90,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error = "no observation"
    while time.monotonic() < deadline:
        try:
            current = frigate_identity_consumers()
            if previous is not None:
                assert_frigate_consumers_reconnected(previous, current)
            return current
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    expectation = "reconnect" if previous is not None else "connection"
    raise RuntimeError(
        f"Frigate identity consumer {expectation} did not stabilize: {last_error}"
    )


def _frigate_consumer_identities(
    state: dict[str, object],
) -> set[tuple[object, object]]:
    return {
        (consumer.get("id"), consumer.get("remote_addr"))
        for consumer in state.get("consumers") or []
        if isinstance(consumer, dict)
    }


def assert_identity_consumer_kept_stable_url_while_retrying(
    before: dict[str, object],
    current_decoder: dict[str, object],
) -> None:
    before_decoder = before.get("decoder") or {}
    if not isinstance(before_decoder, dict) or not isinstance(current_decoder, dict):
        raise RuntimeError("identity consumer retry status is missing during outage")
    for field in ("wrapper_id", "url_sha256", "container_pid"):
        if not before_decoder.get(field) or before_decoder.get(field) != current_decoder.get(field):
            raise RuntimeError(f"camera outage changed stable client field {field}")
    if int(current_decoder.get("attempt") or 0) < int(before_decoder.get("attempt") or 0):
        raise RuntimeError("identity consumer retry counter moved backwards")
    if int(current_decoder.get("total_frames") or 0) < int(
        before_decoder.get("total_frames") or 0
    ):
        raise RuntimeError("identity consumer lost decoded-frame history while retrying")


def assert_frigate_consumers_reconnected(
    before: dict[str, object],
    after: dict[str, object],
) -> None:
    if not before.get("stream_key") or before.get("stream_key") != after.get("stream_key"):
        raise RuntimeError("Frigate reconnected through a different CamAdmiral stream name")

    old = _frigate_consumer_identities(before)
    new = _frigate_consumer_identities(after)
    if not old or not new or old & new:
        raise RuntimeError(
            "Frigate did not replace every affected CamAdmiral RTSP connection: "
            f"before={sorted(old)}, after={sorted(new)}"
        )


def _receiver_children(producer: dict[str, object]) -> dict[int, set[int]]:
    return {
        int(receiver["id"]): {
            int(child) for child in receiver.get("children") or []
        }
        for receiver in producer.get("receivers") or []
        if isinstance(receiver, dict) and receiver.get("id")
    }


def _assert_sender_links(
    senders: dict[int, dict[str, object]],
    producer: dict[str, object],
    *,
    phase: str,
) -> dict[int, int]:
    receiver_children = _receiver_children(producer)
    parents: dict[int, int] = {}
    for sender_id, sender in senders.items():
        parent = int(sender.get("parent") or 0)
        if sender_id not in receiver_children.get(parent, set()):
            raise RuntimeError(
                f"identity sender {sender_id} is not reciprocally linked to its "
                f"{phase} receiver {parent}"
            )
        parents[sender_id] = parent
    return parents


def assert_identity_consumer_reconnected(
    before: dict[str, object],
    after: dict[str, object],
    *,
    expected_old_host: str = "172.30.0.13",
    expected_new_host: str = "172.30.0.15",
    source_path: str = "/main",
) -> None:
    stable_fields = ("stream_key", "go2rtc_pid")
    for field in stable_fields:
        if before.get(field) != after.get(field):
            raise RuntimeError(
                f"relay restart changed stable relay field {field}: "
                f"before={before.get(field)!r}, after={after.get(field)!r}"
            )
    for field in ("id", "remote_addr", "user_agent"):
        if not before.get(field) or not after.get(field) or before.get(field) == after.get(field):
            raise RuntimeError(
                f"relay restart did not replace consumer {field}: "
                f"before={before.get(field)!r}, after={after.get(field)!r}"
            )

    before_senders = {
        int(sender["id"]): sender
        for sender in before.get("senders") or []
        if isinstance(sender, dict) and sender.get("id")
    }
    after_senders = {
        int(sender["id"]): sender
        for sender in after.get("senders") or []
        if isinstance(sender, dict) and sender.get("id")
    }
    if not before_senders or not after_senders or before_senders.keys() == after_senders.keys():
        raise RuntimeError(
            "relay restart did not replace the consumer sender set: "
            f"before={sorted(before_senders)}, after={sorted(after_senders)}"
        )
    video_sender_ids = {
        sender_id
        for sender_id, sender in after_senders.items()
        if sender.get("codec_type") == "video"
    }
    if not video_sender_ids:
        raise RuntimeError("go2rtc identity consumer has no video sender")
    if any(int(after_senders[sender_id].get("packets") or 0) <= 0 for sender_id in video_sender_ids):
        raise RuntimeError("reconnected identity consumer did not receive moved packets")

    before_producer = before.get("producer") or {}
    after_producer = after.get("producer") or {}
    if not isinstance(before_producer, dict) or not isinstance(after_producer, dict):
        raise RuntimeError("go2rtc identity producer topology is missing")
    before_producer_id = before_producer.get("id")
    after_producer_id = after_producer.get("id")
    if (
        not before_producer_id
        or not after_producer_id
        or before_producer_id == after_producer_id
    ):
        raise RuntimeError("relay restart did not create a new upstream producer")
    old_host = str(before_producer.get("url_host") or "")
    new_host = str(after_producer.get("url_host") or "")
    old_path = str(before_producer.get("url_path") or "")
    new_path = str(after_producer.get("url_path") or "")
    old_remote_host = str(before_producer.get("remote_host") or "")
    new_remote_host = str(after_producer.get("remote_host") or "")
    if (
        old_host != expected_old_host
        or new_host != expected_new_host
        or old_path != source_path
        or new_path != source_path
        or old_remote_host != expected_old_host
        or new_remote_host != expected_new_host
    ):
        raise RuntimeError(
            "go2rtc producer did not connect to the expected moved source: "
            f"url={old_host!r}{old_path}->{new_host!r}{new_path}, "
            f"peer={old_remote_host!r}->{new_remote_host!r}"
        )

    _assert_sender_links(before_senders, before_producer, phase="original")
    _assert_sender_links(after_senders, after_producer, phase="reconnected")

    before_decoder = before.get("decoder") or {}
    after_decoder = after.get("decoder") or {}
    if not isinstance(before_decoder, dict) or not isinstance(after_decoder, dict):
        raise RuntimeError("identity consumer decode status is missing")
    if before_decoder.get("status") != "running" or after_decoder.get("status") != "running":
        raise RuntimeError("identity decoder is not running before and after reconnect")
    for field in ("wrapper_id", "url_sha256", "container_pid"):
        if not before_decoder.get(field) or before_decoder.get(field) != after_decoder.get(field):
            raise RuntimeError(f"relay restart changed stable client field {field}")
    for field in ("session_id", "user_agent", "consumer_pid"):
        if not before_decoder.get(field) or before_decoder.get(field) == after_decoder.get(field):
            raise RuntimeError(f"relay restart did not replace client field {field}")
    before_attempt = int(before_decoder.get("attempt") or 0)
    after_attempt = int(after_decoder.get("attempt") or 0)
    if after_attempt <= before_attempt:
        raise RuntimeError(
            "camera recovery did not reconnect the downstream client: "
            f"before={before_attempt}, after={after_attempt}"
        )
    if before_decoder.get("user_agent") != before.get("user_agent"):
        raise RuntimeError("original go2rtc topology does not belong to the original decoder")
    if after_decoder.get("user_agent") != after.get("user_agent"):
        raise RuntimeError("reconnected go2rtc topology does not belong to the new decoder")
    if int(after_decoder.get("frames") or 0) < 5:
        raise RuntimeError("reconnected identity decoder did not receive enough moved frames")


def assert_identity_consumer_keeps_advancing(
    previous: dict[str, object],
    current: dict[str, object],
) -> None:
    for field in ("stream_key", "go2rtc_pid", "id", "remote_addr", "user_agent"):
        if previous.get(field) != current.get(field):
            raise RuntimeError(f"identity consumer changed {field} after reconnect")
    previous_senders = {
        int(sender["id"]): sender
        for sender in previous.get("senders") or []
        if isinstance(sender, dict) and sender.get("id")
    }
    current_senders = {
        int(sender["id"]): sender
        for sender in current.get("senders") or []
        if isinstance(sender, dict) and sender.get("id")
    }
    if previous_senders.keys() != current_senders.keys():
        raise RuntimeError("identity consumer sender set changed after reconnect")
    video_sender_ids = {
        sender_id
        for sender_id, sender in current_senders.items()
        if sender.get("codec_type") == "video"
    }
    if not video_sender_ids or any(
        int(current_senders[sender_id].get("packets") or 0)
        <= int(previous_senders[sender_id].get("packets") or 0)
        for sender_id in video_sender_ids
    ):
        raise RuntimeError("identity consumer media stopped advancing after reconnect")
    previous_producer = previous.get("producer") or {}
    current_producer = current.get("producer") or {}
    if (
        not isinstance(previous_producer, dict)
        or not isinstance(current_producer, dict)
        or previous_producer.get("id") != current_producer.get("id")
        or current_producer.get("url_host") != "172.30.0.15"
        or current_producer.get("url_path") != "/main"
        or current_producer.get("remote_host") != "172.30.0.15"
    ):
        raise RuntimeError("replacement producer did not remain connected")
    _assert_sender_links(current_senders, current_producer, phase="current")

    previous_decoder = previous.get("decoder") or {}
    current_decoder = current.get("decoder") or {}
    if not isinstance(previous_decoder, dict) or not isinstance(current_decoder, dict):
        raise RuntimeError("identity consumer decode status is missing")
    for field in (
        "wrapper_id",
        "url_sha256",
        "attempt",
        "session_id",
        "user_agent",
        "consumer_pid",
        "container_pid",
    ):
        if previous_decoder.get(field) != current_decoder.get(field):
            raise RuntimeError(f"identity decoder changed {field} after reconnect")
    if (
        current_decoder.get("status") != "running"
        or int(current_decoder.get("frames") or 0)
        <= int(previous_decoder.get("frames") or 0)
        or int(current_decoder.get("total_frames") or 0)
        <= int(previous_decoder.get("total_frames") or 0)
        or float(current_decoder.get("last_frame_at") or 0)
        <= float(previous_decoder.get("last_frame_at") or 0)
    ):
        raise RuntimeError("identity decoder stopped producing fresh frames after reconnect")
    previous_fingerprint = previous_decoder.get("fingerprint") or []
    current_fingerprint = current_decoder.get("fingerprint") or []
    try:
        distance = fingerprint_distance(previous_fingerprint, current_fingerprint)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("identity decoder fingerprint is unavailable") from exc
    if distance > 8:
        raise RuntimeError(
            f"identity decoder changed away from moved media after reconnect ({distance:.2f})"
        )


def wait_for_identity_consumer_advancement(
    previous: dict[str, object], *, timeout: float = 15
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error = "no observation"
    while time.monotonic() < deadline:
        try:
            current = identity_consumer()
            assert_identity_consumer_keeps_advancing(previous, current)
            return current
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(
        "identity consumer did not keep decoding moved media after reconnect: "
        f"{last_error}"
    )


def ui_scenario(name: str | None = None) -> None:
    published = run("port", "camadmiral", "18080", capture=True)
    if not published:
        raise RuntimeError("CamAdmiral E2E web port was not published")
    address = published.splitlines()[0].replace("0.0.0.0:", "127.0.0.1:")
    environment = dict(os.environ)
    environment.update(
        {
            "CAMADMIRAL_E2E_WEB_URL": f"http://{address}",
            "CAMADMIRAL_E2E_ADMIN_PASSWORD": "synthetic-e2e-admin-password",
        }
    )
    command = [sys.executable, str(ROOT / "e2e" / "ui.py")]
    if name is not None:
        command.append(name)
    subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=True,
    )


def main() -> int:
    keep = os.environ.get("CAMADMIRAL_E2E_KEEP") == "1"
    identity_started = False
    started = time.monotonic()
    ARTIFACT_DIR.mkdir(exist_ok=True)
    TRANSCRIPT.write_text("", encoding="utf-8")
    try:
        run("down", "--volumes", "--remove-orphans")
        run("build", "camadmiral")
        run(
            "up", "--detach", "camadmiral", "camera-open", "camera-auth",
            "camera-onvif", "camera-secondary", "camera-onvif-large",
            "camera-rtsp-large", "rtsp-bridge",
        )
        run(
            "exec", "-T", "camadmiral", "python", "/e2e/faults.py",
            "seed-scan-pid-pressure",
        )
        scenario("multi-subnet-discovery")
        run(
            "exec", "-T", "camadmiral", "python", "/e2e/faults.py",
            "clear-scan-pid-pressure",
        )
        scenario("partial-subnet-preservation")
        run(
            "exec", "-T", "camadmiral", "python", "-c",
            "import socket; socket.create_connection(('172.29.0.88', 554), 2).close()",
        )
        scenario("large-subnet-multicast-discovery")
        scenario("configured-routed-subnet-discovery")
        ui_scenario("direct-rtsp")
        scenario("direct-rtsp-created")
        run("up", "--detach", "frigate", "frigate-api-proxy")
        scenario("direct-rtsp-frigate")
        run("restart", "camadmiral")
        run("restart", "frigate-api-proxy")
        scenario("direct-rtsp-after-restart")
        run("stop", "rtsp-bridge")
        run("rm", "--force", "rtsp-bridge")
        run("up", "--detach", "rtsp-bridge-moved")
        scenario("direct-rtsp-dns-move")
        run("stop", "rtsp-bridge-moved")
        run("rm", "--force", "rtsp-bridge-moved")
        run("up", "--detach", "rtsp-bridge-faulted")
        scenario("direct-rtsp-path-failure")
        run("stop", "rtsp-bridge-faulted")
        run("rm", "--force", "rtsp-bridge-faulted")
        run("up", "--detach", "rtsp-bridge-moved")
        scenario("direct-rtsp-path-recovery")
        run("stop", "frigate-api-proxy", "frigate")
        run("down", "--volumes", "--remove-orphans")
        run(
            "up", "--detach", "camadmiral", "camera-open", "camera-auth",
            "camera-onvif",
        )
        scenario("baseline")
        ui_scenario()
        scenario("accept-ui-lifecycle-state")
        run("up", "--detach", "frigate", "frigate-api-proxy")
        scenario("frigate")
        run("restart", "frigate")
        scenario("frigate-restart-verify")
        scenario("frigate-unadopt")
        scenario("accept-ui-lifecycle-state")
        run("restart", "frigate")
        scenario("frigate-restart-verify")
        scenario("frigate-ambiguous-delete-setup")
        run(
            "exec", "-T", "frigate", "python3", "-c",
            "from pathlib import Path; import yaml; "
            "path=Path('/config/go2rtc_homekit.yml'); "
            "data=yaml.safe_load(path.read_text()) or {}; "
            "streams=data.get('streams'); "
            "streams.pop('camadmiral_synthetic_ambiguous_delete_detect', None) "
            "if isinstance(streams, dict) else None; "
            "path.write_text(yaml.safe_dump(data, sort_keys=False) if data else '')",
        )
        scenario("frigate-ambiguous-delete-verify")
        run("stop", "frigate-api-proxy", "frigate")

        run("exec", "-T", "camadmiral", "python", "/e2e/faults.py", "delete-managed-stream")
        scenario("runtime-drift")

        container_before = run("ps", "--quiet", "camadmiral", capture=True)
        run("exec", "-T", "camadmiral", "sh", "-c", 'kill "$(pidof go2rtc)"')
        scenario("runtime-recovery")
        container_after = run("ps", "--quiet", "camadmiral", capture=True)
        if not container_before or container_before != container_after:
            raise RuntimeError("CamAdmiral container restarted during the go2rtc child fault")

        run("stop", "camera-open")
        run("restart", "camadmiral")
        scenario("camera-outage")
        run("start", "camera-open")
        scenario("camera-recovery")
        run(
            "exec", "-T", "camadmiral", "python", "/e2e/faults.py",
            "assert-open-camera-stale-scan-summary",
        )

        run("restart", "camadmiral")
        scenario("container-restart")

        run(
            "exec", "-T", "camadmiral", "python", "/e2e/faults.py",
            "set-open-camera-invalid-address",
        )
        scenario("invalid-address")

        run("stop", "camera-open")
        run("rm", "--force", "camera-open")
        run("up", "--detach", "camera-open-moved")
        scenario("moved-camera-ready")
        run(
            "exec",
            "-T",
            "camadmiral",
            "python",
            "/e2e/faults.py",
            "set-open-camera-moved-address",
        )
        scenario("address-recovery")

        run("stop", "camera-auth")
        run("rm", "--force", "camera-auth")
        run("up", "--detach", "camera-auth-rotated")
        scenario("rotated-camera-ready")
        scenario("credential-repair")

        run("up", "--detach", "frigate", "frigate-api-proxy")
        scenario("identity-recovery-setup")
        run("up", "--detach", "identity-consumer", "identity-control-consumer")
        identity_started = True
        scenario("identity-consumer-ready")
        downstream_consumer_before = identity_consumer()
        control_consumer_before = identity_control_consumer()
        frigate_consumers_before = wait_for_frigate_identity_consumers()
        scenario("identity-outage-start")
        run("stop", "camera-onvif", "camera-open-moved")
        run("rm", "--force", "camera-onvif", "camera-open-moved")
        scenario("identity-recovery-missed-scan")
        downstream_decoder_during_outage = identity_decoder_status(
            "identity-consumer",
            "/state/identity-consumer-status.json",
        )
        control_decoder_during_outage = identity_decoder_status(
            "identity-control-consumer",
            "/state/identity-control-status.json",
        )
        assert_identity_consumer_kept_stable_url_while_retrying(
            downstream_consumer_before,
            downstream_decoder_during_outage,
        )
        assert_identity_consumer_kept_stable_url_while_retrying(
            control_consumer_before,
            control_decoder_during_outage,
        )
        scenario("identity-reconnect-checkpoint")
        run("up", "--detach", "camera-onvif-moved", "camera-open-moved-again")
        scenario("identity-recovery")
        downstream_consumer_after = identity_consumer()
        control_consumer_after = identity_control_consumer()
        frigate_consumers_after = wait_for_frigate_identity_consumers(
            previous=frigate_consumers_before
        )
        assert_identity_consumer_reconnected(
            downstream_consumer_before, downstream_consumer_after
        )
        assert_frigate_consumers_reconnected(
            frigate_consumers_before, frigate_consumers_after
        )
        assert_identity_consumer_reconnected(
            control_consumer_before,
            control_consumer_after,
            expected_old_host="172.30.0.12",
            expected_new_host="172.30.0.17",
        )
        wait_for_identity_consumer_advancement(downstream_consumer_after)
        print(
            "identity-consumer-reconnect: go2rtc closed the affected downstream "
            "RTSP session and the client reconnected to the same stable URL"
        )
        ui_scenario("identity-history")
        run(
            "exec", "-T", "camadmiral", "python", "/e2e/faults.py",
            "assert-onvif-runtime-config-moved",
        )
        container_before = run("ps", "--quiet", "camadmiral", capture=True)
        go2rtc_before = run(
            "exec", "-T", "camadmiral", "pidof", "go2rtc", capture=True
        )
        run("exec", "-T", "camadmiral", "sh", "-c", 'kill "$(pidof go2rtc)"')
        scenario("identity-runtime-restart")
        container_after = run("ps", "--quiet", "camadmiral", capture=True)
        go2rtc_after = run(
            "exec", "-T", "camadmiral", "pidof", "go2rtc", capture=True
        )
        if not container_before or container_before != container_after:
            raise RuntimeError(
                "CamAdmiral container restarted during the identity go2rtc child fault"
            )
        if not go2rtc_before or not go2rtc_after or go2rtc_before == go2rtc_after:
            raise RuntimeError("go2rtc did not restart with a distinct child process")

        run("stop", "camera-onvif-moved")
        run("rm", "--force", "camera-onvif-moved")
        run("up", "--detach", "camera-onvif-reidentified")
        scenario("identity-replacement")
        run("down", "--volumes", "--remove-orphans")
        run_launcher()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"CamAdmiral E2E failed: {exc}", file=sys.stderr)
        with TRANSCRIPT.open("a", encoding="utf-8") as transcript:
            transcript.write(f"FAILURE: {exc}\n")
        if identity_started:
            try:
                run(
                    "exec",
                    "-T",
                    "camadmiral",
                    "python",
                    "/e2e/faults.py",
                    "identity-diagnostics",
                    capture=True,
                )
            except (OSError, subprocess.CalledProcessError, RuntimeError):
                pass
            try:
                run(
                    "exec",
                    "-T",
                    "identity-consumer",
                    "cat",
                    "/state/identity-consumer-status.json",
                    capture=True,
                )
            except (OSError, subprocess.CalledProcessError):
                pass
            try:
                run(
                    "exec",
                    "-T",
                    "identity-consumer",
                    "tail",
                    "-n",
                    "20",
                    "/state/identity-consumer-frames.jsonl",
                    capture=True,
                )
            except (OSError, subprocess.CalledProcessError):
                pass
        try:
            logs = run("logs", "--no-color", "--tail", "200", "camadmiral", capture=True)
        except (OSError, subprocess.CalledProcessError):
            logs = ""
        if logs:
            print("Recent CamAdmiral logs:", file=sys.stderr)
            print(logs, file=sys.stderr)
        try:
            frigate_logs = run(
                "logs", "--no-color", "--tail", "200", "frigate", capture=True
            )
        except (OSError, subprocess.CalledProcessError):
            frigate_logs = ""
        if frigate_logs:
            print("Recent Frigate logs:", file=sys.stderr)
            print(frigate_logs, file=sys.stderr)
        if keep:
            print("CAMADMIRAL_E2E_KEEP=1, leaving the isolated lab running", file=sys.stderr)
        return 1
    finally:
        if not keep:
            try:
                run("down", "--volumes", "--remove-orphans")
            except (OSError, subprocess.CalledProcessError):
                pass
    print(f"CamAdmiral E2E passed in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
