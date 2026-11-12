"""Real Frigate import/export policy on current and prior supported releases.

Uses a real synthetic microphone tone and video, never fleet media. The same
assertion must fail on the pinned application baseline and pass on the candidate.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "9e156458827c420d21db565c86159018dc7f880a"


def driver(variant):
    import yaml
    from scenarios import request_json, wait_for, wait_for_health, consumer_directory
    def frigate(path):
        return json.load(urllib.request.urlopen("http://frigate:5000/api" + path, timeout=10))
    def codecs(alias):
        result = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
            "-timeout", "5000000", "-analyzeduration", "1000000", "-show_entries",
            "stream=codec_type", "-of", "json", "rtsp://frigate:8554/" + alias],
            capture_output=True, text=True, timeout=20, check=True)
        return sorted(s["codec_type"] for s in json.loads(result.stdout)["streams"])
    def video(alias):
        result = subprocess.run(["ffmpeg", "-v", "error", "-threads", "1",
            "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", "rtsp://frigate:8554/" + alias,
            "-an", "-frames:v", "5", "-f", "framemd5", "-"],
            capture_output=True, timeout=20, check=True)
        assert sum(bool(l) and not l.startswith(b"#") for l in result.stdout.splitlines()) == 5
    wait_for_health()
    if variant == "persist":
        state = json.loads(Path("/state/audio-policy.json").read_text())
        targets = request_json("/internal/frigate-targets")["targets"]
        assert next(t for t in targets if t["target_id"] == state["target_id"])["audio_mode"] == "video_only"
        assert codecs("legacy_live") == ["video"]
        video("legacy_live")
        print("AUDIO_POLICY_PERSISTENCE_PASSED", flush=True)
        return
    wait_for("Frigate captures synthetic video", lambda: frigate("/stats").get("cameras", {}).get("synthetic_entrance", {}).get("camera_fps", 0) > 0, timeout=180)
    assert codecs("legacy_live") == ["audio", "video"], "Fixture must have actual A/V"
    fixture = json.load(urllib.request.urlopen("http://backchannel-camera:8080/stats", timeout=5))
    assert fixture["empty_audio"] and fixture["dropped_audio_packets"] > 0, "Fixture must advertise audio without forwarding packets"
    original = yaml.safe_load(frigate("/config/raw"))
    target = request_json("/internal/frigate-targets", method="POST", expected=201,
        headers={"X-CamAdmiral-Action": "add-frigate-target"},
        payload={"name": "Synthetic A/V recorder", "api_url": "http://127.0.0.1:20017"})["target"]
    base = "/internal/frigate-targets/" + target["target_id"]
    job = request_json(base + "/import")
    request_json(base + "/import/" + job["id"], method="POST", expected=202,
        headers={"X-CamAdmiral-Action": "import-frigate-cameras"},
        payload={"selected": ["synthetic_entrance"], "allow_restart": True})
    job = wait_for("synthetic import completed", lambda: (j if (j := request_json(base + "/import/" + job["id"]))["status"] == "completed" else False), timeout=240)
    assert job["cameras"][0]["status"] == "applied", job
    observed = codecs("legacy_live")
    if observed != ["video"]:
        assert variant == "baseline" and observed == ["audio", "video"], observed
        print("AUDIO_POLICY_BASELINE_REPRODUCED " + json.dumps({"codecs": observed}), flush=True)
        return
    assert variant == "candidate", "Baseline unexpectedly filters audio"
    assert target["audio_mode"] == "video_only"
    video("legacy_live")
    camera_id = job["cameras"][0]["camera_uuid"]
    signature = [(c["id"], [(s["id"], s["downstream"]["url"]) for s in c["streams"]]) for c in consumer_directory()["cameras"]]
    before = yaml.safe_load(frigate("/config/raw"))
    started = frigate("/stats")["service"]["last_updated"] - frigate("/stats")["service"]["uptime"]
    def update(mode):
        response = request_json(base, method="PUT", headers={"X-CamAdmiral-Action": "update-frigate-target"}, payload={"audio_mode": mode})
        assert response["target"]["audio_mode"] == mode
        def synced():
            raw = yaml.safe_load(frigate("/config/raw"))
            value = raw["go2rtc"]["streams"]["legacy_live"]
            return ("?video" in str(value)) == (mode == "video_only")
        wait_for("owned aliases follow audio policy", synced, timeout=90)
        # Reopen a downstream RTSP reader to use the replaced runtime alias.
        wait_for("media matches saved audio policy", lambda: codecs("legacy_live") == (["video"] if mode == "video_only" else ["audio", "video"]), timeout=60)
    # Start transmitting the advertised microphone only for preservation checks.
    urllib.request.urlopen("http://backchannel-camera:8080/audio/resume", timeout=5).read()
    update("preserve")
    audio = subprocess.run(["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "5000000",
        "-i", "rtsp://frigate:8554/legacy_live", "-vn", "-t", "1", "-ac", "1", "-f", "s16le", "-"], capture_output=True, timeout=20, check=True).stdout
    assert len(audio) > 8000 and any(audio), "Preservation must retain real microphone packets"
    update("video_only")
    urllib.request.urlopen("http://backchannel-camera:8080/audio/stall", timeout=5).read()
    video("legacy_live")
    after = yaml.safe_load(frigate("/config/raw"))
    assert after == before, "Policy round trip changed camera tuning or other configuration"
    assert after["cameras"]["synthetic_entrance"]["motion"] == original["cameras"]["synthetic_entrance"]["motion"]
    assert [(c["id"], [(s["id"], s["downstream"]["url"]) for s in c["streams"]]) for c in consumer_directory()["cameras"]] == signature
    now = frigate("/stats")["service"]
    assert abs(now["last_updated"] - now["uptime"] - started) < 3, "Changing audio policy restarted Frigate"
    # Invalid values must leave the saved choice intact.
    from scenarios import request
    status, _, _ = request(base, method="PUT", headers={"X-CamAdmiral-Action": "update-frigate-target"}, payload={"audio_mode": "invalid"})
    assert status == 422
    assert request_json(base + "/cameras/" + camera_id + "/config")["configuration"].count("?video") >= 2
    Path("/state/audio-policy.json").write_text(json.dumps({"target_id": target["target_id"]}))
    print("AUDIO_POLICY_CANDIDATE_PASSED " + json.dumps({"video_frames": 5, "preserved_audio_bytes": len(audio), "identity_preserved": True, "no_frigate_restart": True}), flush=True)


def host():
    artifacts = ROOT / "e2e-artifacts/frigate-audio"
    artifacts.mkdir(parents=True, exist_ok=True)
    compose = ["docker", "compose", "-p", "camadmiral-frigate-audio", "-f", str(ROOT / "e2e/compose.yaml"), "-f", str(ROOT / "e2e/frigate-audio-compose.yaml")]
    env = dict(os.environ)
    evidence = {"baseline_source": BASELINE, "candidate_source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(), "frigate_version": env.get("FRIGATE_TEST_VERSION", "0.18.2")}
    def run(*args, check=True):
        r = subprocess.run([*compose, *args], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (artifacts / "workload.log").open("a") as log: log.write(r.stdout)
        if check and r.returncode:
            print(r.stdout, flush=True); r.check_returncode()
        return r
    try:
        subprocess.run(["docker", "build", "-t", "camadmiral-e2e:local", str(ROOT)], check=True)
        with tempfile.TemporaryDirectory(prefix="audio-baseline-") as tmp:
            archive = subprocess.Popen(["git", "archive", BASELINE], stdout=subprocess.PIPE)
            subprocess.run(["tar", "-x", "-C", tmp], stdin=archive.stdout, check=True)
            archive.stdout.close(); assert archive.wait() == 0
            subprocess.run(["docker", "build", "-t", "camadmiral-audio:baseline", tmp], check=True)
        for variant in ("baseline", "candidate"):
            env["AUDIO_TEST_IMAGE"] = "camadmiral-audio:baseline" if variant == "baseline" else "camadmiral-e2e:local"
            run("down", "--volumes", "--remove-orphans")
            run("up", "-d", "camera-open", "backchannel-camera", "camadmiral", "frigate", "frigate-api-proxy")
            result = run("run", "--rm", "--entrypoint", "python", "test-driver", "/e2e/frigate_audio.py", variant, check=False)
            (artifacts / (variant + ".log")).write_text(result.stdout)
            (artifacts / (variant + "-containers.log")).write_text(run("logs", "--no-color", check=False).stdout)
            assert result.returncode == 0, result.stdout
            marker = "AUDIO_POLICY_BASELINE_REPRODUCED" if variant == "baseline" else "AUDIO_POLICY_CANDIDATE_PASSED"
            assert marker in result.stdout, result.stdout
            if variant == "candidate":
                run("restart", "camadmiral")
                run("restart", "frigate-api-proxy")
                resumed = run("run", "--rm", "--entrypoint", "python", "test-driver", "/e2e/frigate_audio.py", "persist")
                assert "AUDIO_POLICY_PERSISTENCE_PASSED" in resumed.stdout
            evidence[variant] = {"passed": True, "image": json.loads(subprocess.check_output(["docker", "image", "inspect", env["AUDIO_TEST_IMAGE"], "--format", "{{json .Id}}"], text=True))}
        evidence["passed"] = True
    finally:
        (artifacts / "evidence.json").write_text(json.dumps(evidence, indent=2))
        run("down", "--volumes", "--remove-orphans", check=False)


if __name__ == "__main__":
    driver(sys.argv[1]) if sys.argv[1:] else host()
