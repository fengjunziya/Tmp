"""Check the local video-demo toolchain and, optionally, the OpenAI model.

The report contains capability facts and never includes an API key or exception text.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
from typing import Any
import zlib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_core import Config, EventLogger, ModelClient, load_config
FONT_CANDIDATES = (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf"))


def make_probe_png() -> bytes:
    """Build a 64x64 RGB color card entirely in memory using the PNG spec."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = bytearray()
    for y in range(64):
        rows.append(0)  # PNG filter type 0: no per-row filtering.
        for x in range(64):
            if 22 <= x < 42 and 22 <= y < 42:
                rgb = (218, 155, 83)  # orange square
            elif x < 7 or x >= 57 or y < 7 or y >= 57:
                rgb = (82, 182, 168)  # teal border
            else:
                rgb = (40, 57, 84)  # navy background
            rows.extend(rgb)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows)))
            + chunk(b"IEND", b""))


def run_tool(path: str, *args: str, timeout: int = 15) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [path, *args], cwd=ROOT, capture_output=True, text=True,
            errors="replace", timeout=timeout, check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error_type": type(exc).__name__}


def tool_version(name: str, flag: str = "-version") -> dict[str, Any]:
    path = shutil.which(name)
    if not path:
        return {"available": False, "path": None, "version": None}
    result = run_tool(path, flag)
    first_line = (result.get("stdout") or result.get("stderr") or "").splitlines()
    return {
        "available": bool(result["ok"]),
        "path": path,
        "version": first_line[0].strip() if first_line else None,
        **({"error_type": result["error_type"]} if "error_type" in result else {}),
    }


def package_info(name: str) -> dict[str, Any]:
    try:
        version = importlib.metadata.version(name)
        importlib.import_module(name)
        return {"available": True, "version": version}
    except (importlib.metadata.PackageNotFoundError, ImportError, OSError) as exc:
        return {"available": False, "version": None, "error_type": type(exc).__name__}


def ffmpeg_capabilities(path: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"encoders": {"libx264": False, "aac": False},
                              "filters": {"subtitles": False, "ass": False, "xfade": False}}
    if not path:
        return result
    encoders = run_tool(path, "-hide_banner", "-encoders")
    filters = run_tool(path, "-hide_banner", "-filters")
    if encoders["ok"]:
        listing = encoders["stdout"] + encoders["stderr"]
        for name in result["encoders"]:
            result["encoders"][name] = bool(re.search(rf"(?m)^\s*\S{{6}}\s+{name}\s", listing))
    if filters["ok"]:
        listing = filters["stdout"] + filters["stderr"]
        for name in result["filters"]:
            result["filters"][name] = bool(re.search(rf"(?m)^\s*\S+\s+{name}\s", listing))
    return result


def check_model(config: Config) -> dict[str, Any]:
    """Run one constrained Responses call with an actual image and function tool."""
    started = time.perf_counter()
    try:
        # The tested demo still is preferred. The stdlib color card is a portable fallback.
        demo_still = ROOT / "materials" / "demo" / "still.png"
        image_bytes = demo_still.read_bytes() if demo_still.is_file() else make_probe_png()
        image = base64.b64encode(image_bytes).decode("ascii")
        image_source = "demo_still" if demo_still.is_file() else "generated_color_card"
        logger = EventLogger(ROOT / "reports" / "model_probe_deepseek", stage="preflight")
        client = ModelClient(config, logger)
        response = client.respond(
            purpose="deepseek_image_and_function_probe",
            timeout=30.0,
            input_items=[{"role": "user", "content": [
                {"type": "input_text", "text": "Inspect the image and call report_capability with a brief description of the colors, shapes, and any visible text you actually see."},
                {"type": "input_image", "image_url": f"data:image/png;base64,{image}"},
            ]}],
            tools=[{
                "type": "function", "name": "report_capability",
                "description": "Report visual observations about the supplied image.",
                "parameters": {
                    "type": "object",
                    "properties": {"observed_description": {"type": "string"}},
                    "required": ["observed_description"],
                    "additionalProperties": False,
                },
                "strict": True,
            }],
            tool_choice="auto",
        )
        output = getattr(response, "output", [])
        calls = [item for item in output if getattr(item, "type", None) == "function_call"
                 and getattr(item, "name", None) == "report_capability"]
        function_args: dict[str, Any] | None = None
        if calls:
            try:
                function_args = json.loads(calls[0].arguments)
            except (ValueError, TypeError):
                pass
        usage = getattr(response, "usage", None)
        observed = function_args.get("observed_description") if function_args else None
        return {
            "attempted": True,
            "ok": bool(calls and isinstance(observed, str) and observed.strip()),
            "model_requested": config.model,
            "reasoning_effort_requested": config.reasoning_effort,
            "api_base": config.api_base,
            "credential_env": config.api_key_env,
            "model_returned": getattr(response, "model", None),
            "response_id": getattr(response, "id", None),
            "request_id": getattr(response, "_request_id", None),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "capability": {
                "image_input_sent": True,
                "image_source": image_source,
                "function_call_returned": bool(calls),
                "observed_description": observed,
            },
            "usage": usage.model_dump(mode="json") if usage is not None else None,
            "cost_usd": None,
        }
    except Exception as exc:  # Never persist exception text, which might include credentials.
        return {
            "attempted": True, "ok": False,
            "model_requested": config.model, "reasoning_effort_requested": config.reasoning_effort,
            "api_base": config.api_base, "credential_env": config.api_key_env,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error_type": type(exc).__name__,
            "status_code": getattr(exc, "status_code", None),
            "cost_usd": None,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "env_check.json")
    parser.add_argument("--config", type=Path, default=ROOT / "config.example.json")
    parser.add_argument("--check-model", action="store_true", help="Make one deepseek-flash Responses call")
    args = parser.parse_args()
    config = load_config(args.config)

    ffmpeg = tool_version("ffmpeg")
    fonts = [{"path": str(path), "usable": path.is_file() and path.stat().st_size > 0}
             for path in FONT_CANDIDATES]
    report: dict[str, Any] = {
        "python": {"available": True, "path": sys.executable, "version": sys.version.split()[0]},
        "packages": {name: package_info(name) for name in ("openai", "pydantic")},
        "tools": {"git": tool_version("git", "--version"), "ffmpeg": ffmpeg,
                  "ffprobe": tool_version("ffprobe")},
        "ffmpeg_capabilities": ffmpeg_capabilities(ffmpeg["path"]),
        "chinese_fonts": fonts,
        "credential_env": config.api_key_env,
        "api_key_present": bool(os.environ.get(config.api_key_env)),
        "model_check": check_model(config) if args.check_model else {"attempted": False, "cost_usd": None},
    }
    caps = report["ffmpeg_capabilities"]
    report["ready_local"] = all((
        report["packages"]["openai"]["available"],
        report["packages"]["pydantic"]["available"],
        *(tool["available"] for tool in report["tools"].values()),
        *caps["encoders"].values(), *caps["filters"].values(),
        any(font["usable"] for font in fonts),
    ))
    report["ready_model"] = bool(report["model_check"].get("ok")) if args.check_model else None

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Preflight report: {args.output.resolve()}")
    print(f"Local toolchain ready: {report['ready_local']}; DeepSeek key present: {report['api_key_present']}; model check: {report['ready_model']}")
    return 0 if report["ready_local"] and (not args.check_model or report["ready_model"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
