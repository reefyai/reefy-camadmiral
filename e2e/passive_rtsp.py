"""Real A/V regression against an unchanged baseline and the release candidate."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "28ee79068ea25017fe670bc9a68808d0528aea5c"


def control(path="stats"):
    return json.load(urllib.request.urlopen("http://backchannel-camera:8080/" + path, timeout=5))


def capture(uri, name):
    path = Path("/state/" + name + ".mkv")
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-threads", "1", "-rtsp_transport", "tcp",
             "-timeout", "5000000", "-i", uri, "-t", "10",
             "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "pcm_s16le",
             "-y", str(path)], capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise AssertionError("PASSIVE_MEDIA_FAILED: no complete A/V recording") from None
    assert result.returncode == 0, "PASSIVE_MEDIA_FAILED: A/V recording rejected: " + result.stderr.decode(errors="replace")[-1500:]
    probe = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(path)], timeout=10))
    assert {s["codec_type"] for s in probe["streams"]} == {"video", "audio"}, probe
    assert float(probe["format"]["duration"]) >= 9, probe
    video = subprocess.run(
        ["ffmpeg", "-v", "error", "-threads", "1", "-i", str(path),
         "-an", "-frames:v", "50", "-f", "framemd5", "-"],
        capture_output=True, timeout=15, check=True).stdout
    frames = sum(not line.startswith(b"#") and bool(line.strip()) for line in video.splitlines())
    assert frames >= 50, ("too few decoded frames", frames)
    audio = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-t", "1", "-ac", "1",
         "-f", "s16le", "-"], capture_output=True, timeout=10, check=True).stdout
    samples = struct.unpack("<" + "h" * (len(audio) // 2), audio)
    rms = math.sqrt(sum(x*x for x in samples) / max(1, len(samples)))
    assert rms > 100, ("microphone audio lost or silent", rms)
    return {"frames": frames, "duration": probe["format"]["duration"],
            "audio_rms": round(rms), "bytes": path.stat().st_size}


def driver(stage):
    from scenarios import (authenticated_rtsp_url, camera_by_name, consumer_directory,
                           directory_signature, request, request_json, wait_for, wait_for_health)
    state = Path("/state/passive-camera.json")
    wait_for_health()
    wait_for("synthetic camera", control)
    if stage == "setup":
        # Fixture preflight excludes a broken generator or proxy as the baseline cause.
        preflight = capture("rtsp://backchannel-camera:8554/faulty", "direct-preflight")
        assert control()["broken_setups"] == 0, "Plain FFmpeg must not request talkback"
        request_json("/internal/cameras/rtsp", method="POST", expected=201,
                     payload={"display_name": "Synthetic passive camera", "sources": [
                         {"label": "Main", "url": "rtsp://backchannel-camera:8554/faulty"}]},
                     headers={"X-CamAdmiral-Action": "create-direct-rtsp-camera"})
        camera = camera_by_name(consumer_directory(), "Synthetic passive camera")
        state.write_text(json.dumps({"camera": camera, "signature": directory_signature(consumer_directory())}))
        print("PREFLIGHT " + json.dumps(preflight), flush=True)
    saved = json.loads(state.read_text())
    camera = saved["camera"]
    assert directory_signature(consumer_directory()) == saved["signature"], "Persistent identity, roles or URLs changed"
    stream = next(s for s in camera["streams"] if "record" in s["roles"])
    uri = authenticated_rtsp_url(stream)
    with ThreadPoolExecutor(max_workers=2) as pool:
        recordings = list(pool.map(lambda i: capture(uri, stage + "-" + str(i)), range(2)))
    current = control()
    assert current["backchannel_requests"] == 0 and current["broken_setups"] == 0, current
    assert current["max_active"] == 1, ("fan-out opened duplicate camera playback sessions", current)
    print("RECORDINGS " + json.dumps(recordings), flush=True)
    if stage == "verify":
        request_json("/internal/cameras/rtsp", method="POST", expected=201,
                     payload={"display_name": "Synthetic ordinary camera", "sources": [
                         {"label": "Main", "url": "rtsp://backchannel-camera:8554/healthy"}]},
                     headers={"X-CamAdmiral-Action": "create-direct-rtsp-camera"})
        healthy = camera_by_name(consumer_directory(), "Synthetic ordinary camera")
        capture(authenticated_rtsp_url(healthy["streams"][0]), "ordinary")
        u = urlsplit(uri)
        wrong = urlunsplit(u._replace(netloc="invalid:invalid@" + u.hostname + ":" + str(u.port)))
        denied = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                                 wrong], capture_output=True, timeout=12)
        assert denied.returncode != 0, "Wrong downstream credentials accepted"
        control("stall")
        try:
            status, _, _ = request(f"/internal/cameras/{camera['id']}/snapshot.jpg", timeout=12)
            assert status == 503, ("Missing video was reported as a decoded frame", status)
            try:
                capture(uri, "missing-video")
            except AssertionError as exc:
                assert "PASSIVE_MEDIA_FAILED" in str(exc), str(exc)
            else:
                raise AssertionError("Missing video incorrectly passed A/V validation")
        finally:
            control("resume")
        capture(uri, "recovered")
        assert directory_signature(consumer_directory())[camera["id"]] == saved["signature"][camera["id"]]
    print("PASS: receive-only video and microphone audio, stable identity, bounded readers", flush=True)


def host():
    artifacts = ROOT / "e2e-artifacts" / "passive-rtsp"
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ["docker", "compose", "-p", "camadmiral-passive-e2e",
               "-f", str(ROOT / "e2e/compose.yaml"),
               "-f", str(ROOT / "e2e/passive-rtsp-compose.yaml")]
    evidence = {"baseline_source": BASELINE,
                "candidate_source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    env = dict(os.environ)
    def run(*args, check=True):
        r = subprocess.run([*compose, *args], env=env, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (artifacts / "workload.log").open("a") as log:
            log.write(r.stdout)
        if check and r.returncode:
            print(r.stdout, flush=True)
            r.check_returncode()
        return r
    def stats():
        output = run("exec", "-T", "backchannel-camera", "python", "-c",
                     "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/stats').read().decode())").stdout
        return json.loads(output)
    try:
        subprocess.run(["docker", "build", "-t", "camadmiral-e2e:local", str(ROOT)], check=True)
        with tempfile.TemporaryDirectory(prefix="camadmiral-baseline-") as temp:
            archive = subprocess.Popen(["git", "archive", BASELINE], cwd=ROOT, stdout=subprocess.PIPE)
            subprocess.run(["tar", "-x", "-C", temp], stdin=archive.stdout, check=True)
            archive.stdout.close()
            assert archive.wait() == 0
            subprocess.run(["docker", "build", "-t", "camadmiral-e2e:baseline", temp], check=True)
        evidence["images"] = [
            json.loads(line) for line in subprocess.check_output(
                ["docker", "image", "inspect", "camadmiral-e2e:local", "camadmiral-e2e:baseline",
                 "--format", "{{json .Id}}"], text=True).splitlines()
        ]
        for variant in ("baseline", "candidate"):
            env["PASSIVE_TEST_IMAGE"] = "camadmiral-e2e:" + ("baseline" if variant == "baseline" else "local")
            run("down", "--volumes", "--remove-orphans")
            run("up", "-d", "camera-open", "backchannel-camera", "camadmiral")
            result = run("run", "--rm", "--entrypoint", "python", "test-driver",
                         "/e2e/passive_rtsp.py", "setup", check=False)
            point = {"returncode": result.returncode, "camera": stats()}
            evidence[variant] = point
            (artifacts / (variant + "-driver.log")).write_text(result.stdout)
            (artifacts / (variant + "-containers.log")).write_text(
                run("logs", "--no-color", "camadmiral", "backchannel-camera", check=False).stdout)
            if variant == "baseline":
                assert result.returncode != 0 and "PASSIVE_MEDIA_FAILED" in result.stdout, result.stdout
                assert point["camera"]["broken_setups"] > 0, point
                point["expected_failure_confirmed"] = True
            else:
                assert result.returncode == 0, result.stdout
                run("restart", "camadmiral")
                run("run", "--rm", "--entrypoint", "python", "test-driver",
                    "/e2e/passive_rtsp.py", "verify")
                point["restart_and_negative_checks_passed"] = True
                point["final_camera"] = stats()
            print(variant + " " + json.dumps(point), flush=True)
        evidence["passed"] = True
    finally:
        (artifacts / "evidence.json").write_text(json.dumps(evidence, indent=2))
        (artifacts / "final-containers.log").write_text(run("logs", "--no-color", check=False).stdout)
        run("down", "--volumes", "--remove-orphans", check=False)


if __name__ == "__main__":
    driver(sys.argv[1]) if sys.argv[1:] else host()
