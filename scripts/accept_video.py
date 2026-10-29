"""Immutable, small end-to-end acceptance gate for the authorized demo."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "runs" / "demo"


def read(name):
    return json.loads((RUN / name).read_text(encoding="utf-8"))


def main():
    required_sources = ["video_agent.py", "media.py", "timeline.py", "renderer.py", "verify.py"]
    for name in required_sources:
        source = ROOT / name
        assert source.is_file(), f"Missing source: {name}"
        compile(source.read_text(encoding="utf-8"), name, "exec")
    for name in ["assets.json", "analysis.json", "requirements_v1.json", "requirements_v2.json", "revision_diff.json", "manifest.json", "usage.json", "events.jsonl"]:
        assert (RUN / name).is_file(), f"Missing delivery evidence: {name}"
    assets = read("assets.json")
    rows = assets["assets"] if isinstance(assets, dict) else assets
    kinds = {x["asset_id"]: x["kind"] for x in rows}
    assert {"video", "image", "audio"} <= set(kinds.values()), "Probe all three media kinds"
    for version, title, first in [(1, "十秒灵感", "video"), (2, "让灵感再快一步", "image")]:
        t = read(f"timeline_v{version}.json")
        assert t["version"] == version
        assert t["parent_version"] == (None if version == 1 else 1)
        assert t["output"]["width"] == 1280 and t["output"]["height"] == 720
        assert t["output"]["fps"] == 30
        clips = t["clips"]
        assert len(clips) >= 2 and kinds[clips[0]["asset_id"]] == first, "Required clip order"
        assert any(c["transition_out"]["type"] == "dissolve" and c["transition_out"]["duration_ms"] == 500 for c in clips[:-1])
        duration = sum(c["out_ms"]-c["in_ms"] if c["kind"] == "video" else c["duration_ms"] for c in clips)
        duration -= sum(c["transition_out"]["duration_ms"] for c in clips[:-1] if c["transition_out"]["type"] == "dissolve")
        assert duration == 10000, f"Timeline duration {duration} != 10000"
        assert any(c["text"] == title and c["start_ms"] == 0 and c["end_ms"] == 3000 for c in t["captions"])
        assert any(c["text"] == "由视频 Agent 自动剪辑" and c["start_ms"] == 8000 and c["end_ms"] == 10000 for c in t["captions"])
        assert t["bgm"] and t["bgm"]["gain_db"] == -20
        for c in clips:
            assert c["fit"] == "pad"
            if c["kind"] == "video":
                assert c["gain_db"] == -18 and not c["mute"]
        report = read(f"validation_v{version}.json")
        assert report["status"] == "pass", f"Version {version}: {report.get('status')}"
        movie = RUN / f"output_v{version}.mp4"
        assert movie.is_file() and movie.stat().st_size > 10000
        p = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(movie)], capture_output=True, text=True, timeout=20)
        assert p.returncode == 0, p.stderr
        probe = json.loads(p.stdout)
        v = next(s for s in probe["streams"] if s["codec_type"] == "video")
        a = next(s for s in probe["streams"] if s["codec_type"] == "audio")
        assert v["width"] == 1280 and v["height"] == 720 and v["codec_name"] == "h264"
        assert a["codec_name"] == "aac"
        assert abs(float(v.get("duration", probe["format"]["duration"])) - 10) <= 0.1
    assert hashlib.sha256((RUN / "output_v1.mp4").read_bytes()).hexdigest() != hashlib.sha256((RUN / "output_v2.mp4").read_bytes()).hexdigest()
    print("VIDEO_DEMO_ACCEPTED: two independently checked 10s revisions")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"VIDEO_DEMO_REJECTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
