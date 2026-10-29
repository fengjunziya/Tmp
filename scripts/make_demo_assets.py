"""Create small, deterministic synthetic media for a ten-second demo run.

Existing files are never removed or overwritten. A complete manifest is reusable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("source_10s.mp4", "still.png", "bgm.wav")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(argv: list[str], timeout: int = 120) -> None:
    completed = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                               errors="replace", timeout=timeout, check=False)
    if completed.returncode:
        tail = completed.stderr[-3000:]
        raise RuntimeError(f"FFmpeg failed with exit {completed.returncode}:\n{tail}")


def duration(path: Path, ffprobe: str) -> float:
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        cwd=ROOT, capture_output=True, text=True, errors="replace", timeout=20, check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"ffprobe failed for {path.name}")
    return float(completed.stdout.strip())


def build_commands(ffmpeg: str, output: Path) -> dict[str, list[str]]:
    # Three 100-frame scenes at 30 fps yield exactly 300 frames / 10 seconds.
    font = str(Path("C:/Windows/Fonts/msyh.ttc")).replace("\\", "/").replace(":", r"\\\:")
    colors = ("0x203a63", "0x674426", "0x23604c")
    video_input: list[str] = []
    for color in colors:
        video_input += ["-f", "lavfi", "-i", f"color=c={color}:s=1280x720:r=30:d=3.333333333"]
    video_input += [
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=10",
    ]
    video_filter = (
        "[0:v]drawbox=x=44:y=44:w=1192:h=632:color=white@0.22:t=8,"
        f"drawtext=fontfile={font}:text=DEMO_01:fontcolor=white:fontsize=70:x=80:y=100[v0];"
        "[1:v]drawbox=x=44:y=44:w=1192:h=632:color=white@0.22:t=8,"
        f"drawtext=fontfile={font}:text=DEMO_02:fontcolor=white:fontsize=70:x=80:y=100[v1];"
        "[2:v]drawbox=x=44:y=44:w=1192:h=632:color=white@0.22:t=8,"
        f"drawtext=fontfile={font}:text=DEMO_03:fontcolor=white:fontsize=70:x=80:y=100[v2];"
        "[v0][v1][v2]concat=n=3:v=1:a=0[background];"
        "[background][3:v]overlay=x='100+40*sin(t*2)':y=370:shortest=1,"
        "format=yuv420p[v];[4:a]volume=0.06[a]"
    )
    video = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-n", *video_input,
        "-filter_complex", video_filter, "-map", "[v]", "-map", "[a]",
        "-frames:v", "300", "-t", "10", "-c:v", "libx264", "-preset", "fast",
        "-crf", "28", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac",
        "-b:a", "96k", "-movflags", "+faststart", "-map_metadata", "-1",
        str(output / "source_10s.mp4"),
    ]
    still = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-f", "lavfi",
        "-i", "color=c=0x283954:s=1280x720:r=1:d=1",
        "-vf", "drawbox=x=60:y=60:w=1160:h=600:color=0x52b6a8@0.85:t=12,"
        "drawbox=x=115:y=340:w=500:h=160:color=0xd99b53@0.9:t=fill,"
        f"drawtext=fontfile={font}:text=DEMO_STILL:fontcolor=white:fontsize=72:x=120:y=120",
        "-frames:v", "1", "-threads", "1", "-update", "1",
        str(output / "still.png"),
    ]
    bgm = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-n",
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=329.63:sample_rate=48000:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=10",
        "-filter_complex", "[0:a][1:a][2:a]amix=inputs=3:normalize=0,volume=0.06,"
        "afade=t=in:st=0:d=0.4,afade=t=out:st=9.4:d=0.6[a]",
        "-map", "[a]", "-t", "10", "-c:a", "pcm_s16le", "-ar", "48000",
        str(output / "bgm.wav"),
    ]
    return {"source_10s.mp4": video, "still.png": still, "bgm.wav": bgm}


def existing_matches(output: Path) -> bool:
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return all((output / name).is_file() and
                   sha256(output / name) == manifest["assets"][name]["sha256"]
                   for name in ASSETS)
    except (OSError, KeyError, TypeError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "materials" / "demo")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    args = parser.parse_args()
    if existing_matches(args.output):
        print(f"Reused verified demo assets in {args.output.resolve()}")
        for name in ASSETS:
            print(f"  {name}")
        return 0
    if not args.ffmpeg:
        parser.error("FFmpeg was not found; pass --ffmpeg PATH")
    ffprobe = shutil.which("ffprobe") or str(Path(args.ffmpeg).with_name("ffprobe.exe"))
    if not Path(ffprobe).is_file():
        parser.error("ffprobe was not found")
    args.output.mkdir(parents=True, exist_ok=True)
    existing = [name for name in (*ASSETS, "manifest.json") if (args.output / name).exists()]
    if existing:
        parser.error(f"Refusing to overwrite existing unverified output: {', '.join(existing)}")

    commands = build_commands(args.ffmpeg, args.output)
    for name in ASSETS:
        run(commands[name])
    assets: dict[str, Any] = {}
    for name in ASSETS:
        path = args.output / name
        measured_duration = duration(path, ffprobe) if name != "still.png" else None
        assets[name] = {
            "synthetic": True,
            "kind": {"source_10s.mp4": "video", "still.png": "image",
                     "bgm.wav": "audio"}[name],
            "duration_seconds": measured_duration,
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "command": commands[name],
        }
    if abs(assets["source_10s.mp4"]["duration_seconds"] - 10.0) > 0.02:
        raise RuntimeError("Source video duration is not ten seconds")
    if abs(assets["bgm.wav"]["duration_seconds"] - 10.0) > 0.02:
        raise RuntimeError("BGM duration is not ten seconds")
    manifest = {"description": "Deterministic, entirely synthetic ten-second demo inputs",
                "assets": assets}
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Created demo assets in {args.output.resolve()}")
    for name in ASSETS:
        print(f"  {name}: {assets[name]['bytes']} bytes, sha256={assets[name]['sha256']}, duration={assets[name]['duration_seconds']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
