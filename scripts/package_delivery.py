"""Package a validated DeepSeek delivery without altering existing evidence.

The archive contains source, demo inputs, reports, the selected two-revision
run, and separately attributed historical runs. No files are removed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_RUNS = {
    "stage_a": "openai_historical",
    "build_video": "openai_historical",
    "final_hardening": "openai_historical",
    "deepseek_migration": "deepseek",
    "credential_guard": "no_api_call_credential_check",
    "demo": "openai_historical",
}
EXCLUDED_DIRS = {".git", ".local", ".venv", "node_modules", "__pycache__"}
EXCLUDED_NAMES = {"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
EXCLUDED_SUFFIXES = {".pem", ".p12", ".pfx", ".key", ".jks", ".keystore"}
SECRET_ENV_NAME = re.compile(r"API_KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)


def inside_workspace(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError(f"Path must remain inside workspace: {path}")
    return resolved


def safe_relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def excluded(path: Path) -> bool:
    name = path.name.lower()
    if name in EXCLUDED_DIRS or name in EXCLUDED_NAMES:
        return True
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return True
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    if name.startswith("credentials") and path.suffix.lower() in {".json", ".txt"}:
        return True
    if name.startswith("secret") and path.suffix.lower() in {".json", ".txt"}:
        return True
    return False


def walk_files(start: Path):
    """Yield files in stable order without following any symbolic link."""
    if start.is_symlink() or excluded(start):
        return
    if start.is_file():
        yield start
        return
    if not start.is_dir():
        return
    with os.scandir(start) as iterator:
        entries = sorted(iterator, key=lambda entry: entry.name)
    for entry in entries:
        path = Path(entry.path)
        if entry.is_symlink() or excluded(path):
            continue
        if entry.is_dir(follow_symlinks=False):
            yield from walk_files(path)
        elif entry.is_file(follow_symlinks=False):
            yield path


def required_run(run_dir: Path) -> dict:
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError(f"Selected run is missing or a symlink: {safe_relative(run_dir)}")
    revisions = {}
    for version in (1, 2):
        required = [
            run_dir / f"output_v{version}.mp4",
            run_dir / f"timeline_v{version}.json",
            run_dir / f"validation_v{version}.json",
        ]
        missing = [safe_relative(path) for path in required if path.is_symlink() or not path.is_file()]
        if missing:
            raise ValueError("Missing selected-run evidence: " + ", ".join(missing))
        if required[0].stat().st_size == 0:
            raise ValueError(f"Empty selected-run movie: {safe_relative(required[0])}")
        try:
            timeline = json.loads(required[1].read_text(encoding="utf-8"))
            validation = json.loads(required[2].read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid selected-run evidence for v{version}: {type(exc).__name__}") from exc
        if timeline.get("version") != version:
            raise ValueError(f"Timeline version mismatch: {safe_relative(required[1])}")
        if validation.get("status") != "pass":
            raise ValueError(f"Validation is not pass: {safe_relative(required[2])}")
        frames = [
            path for path in walk_files(run_dir)
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            and f"v{version}" in path.name.lower()
        ]
        if not frames:
            raise ValueError(f"No evidence frame found for v{version} in {safe_relative(run_dir)}")
        revisions[f"v{version}"] = {
            "status": "validated",
            "output": safe_relative(required[0]),
            "timeline": safe_relative(required[1]),
            "validation": safe_relative(required[2]),
            "evidence_frames": len(frames),
        }
    usage_path = run_dir / "usage.json"
    if usage_path.is_symlink() or not usage_path.is_file():
        raise ValueError(f"Missing selected-run attribution: {safe_relative(usage_path)}")
    try:
        usage = json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid selected-run usage: {safe_relative(usage_path)}") from exc
    if usage.get("provider") != "deepseek" or usage.get("model") != "deepseek-flash":
        raise ValueError(f"Selected run does not identify DeepSeek/deepseek-flash: {safe_relative(usage_path)}")
    return revisions


def secret_values() -> tuple[bytes, ...]:
    values = {
        value.encode("utf-8") for name, value in os.environ.items()
        if SECRET_ENV_NAME.search(name) and len(value) >= 8
    }
    return tuple(sorted(values, key=len, reverse=True))


def hash_and_screen(path: Path, secrets: tuple[bytes, ...]) -> tuple[str, int]:
    needles = (*secrets, b"-----BEGIN PRIVATE KEY-----")
    overlap = max(map(len, needles)) - 1
    digest = hashlib.sha256()
    size = 0
    tail = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            window = tail + chunk
            if any(needle in window for needle in needles):
                raise ValueError(f"Sensitive value found in: {safe_relative(path)}")
            tail = window[-overlap:] if overlap else b""
    return digest.hexdigest(), size


def unique_output(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for number in range(1, 10000):
        candidate = path.with_name(f"{path.stem}_{stamp}_{number}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not find a free output name")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("delivery/Tmp_delivery.zip"))
    parser.add_argument("--run-dir", type=Path, default=Path("runs/deepseek_video"))
    parser.add_argument("--include-history", action="store_true", help="Include an additional git history.bundle")
    args = parser.parse_args()

    output = inside_workspace(ROOT / args.output)
    if output.suffix.lower() != ".zip":
        raise ValueError("--output must end in .zip")
    run_dir = inside_workspace(ROOT / args.run_dir)
    if not run_dir.is_relative_to(ROOT / "runs") or run_dir == ROOT / "runs":
        raise ValueError("--run-dir must be inside runs/")
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    runtime = {"provider": "deepseek", "model": config.get("model"),
               "endpoint": config.get("api_base"), "credential_env": config.get("api_key_env")}
    if runtime != {"provider": "deepseek", "model": "deepseek-flash",
                   "endpoint": "https://api.deepseek.com", "credential_env": "DEEPSEEK_API_KEY"}:
        raise ValueError("Current runtime configuration differs from the required DeepSeek API policy")
    revisions = required_run(run_dir)

    roots = [
        *(path for path in ROOT.glob("*.py") if path.is_file()),
        *(ROOT / name for name in ("AGENTS.md", "README.md", "requirements.txt",
                                      "config.example.json", "completion_report.txt",
                                      ".gitignore", ".gitattributes")),
        *(ROOT / name for name in ("docs", "tasks", "tests", "scripts", "reports", "examples")),
        ROOT / "materials" / "demo",
        run_dir,
    ]
    for name in HISTORICAL_RUNS:
        historical = ROOT / "runs" / name
        if historical != run_dir:
            if not historical.is_dir() or historical.is_symlink():
                raise ValueError(f"Historical run missing: {safe_relative(historical)}")
            roots.append(historical)
    for root in roots:
        if not root.exists():
            raise ValueError(f"Required delivery path missing: {safe_relative(root)}")

    files = {safe_relative(path): path for root in roots for path in walk_files(root)}
    files = {name: path for name, path in files.items()
             if not name.startswith("delivery/") and path != output}
    secrets = secret_values()
    entries = []
    for name, path in sorted(files.items()):
        digest, size = hash_and_screen(path, secrets)
        parts = Path(name).parts
        provider = ("deepseek" if path.is_relative_to(run_dir) else
                    HISTORICAL_RUNS.get(parts[1], "project_context")
                    if len(parts) > 1 and parts[0] == "runs" else "project_context")
        entries.append({"path": name, "sha256": digest, "size": size, "provider": provider})

    output.parent.mkdir(parents=True, exist_ok=True)
    output = unique_output(output)
    bundle = None
    if args.include_history:
        bundle = output.with_name(output.stem + ".history.bundle")
        if bundle.exists():
            raise ValueError(f"History bundle already exists: {bundle}")
        result = subprocess.run(["git", "bundle", "create", str(bundle), "--all"],
                                cwd=ROOT, capture_output=True, text=True)
        if result.returncode != 0 or not bundle.is_file():
            raise RuntimeError("git bundle create failed; delivery archive was not created")
        digest, size = hash_and_screen(bundle, secrets)
        entries.append({"path": "history.bundle", "sha256": digest, "size": size,
                        "provider": "mixed_historical_git"})

    manifest = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "current_runtime": runtime,
        "selected_run": {"path": safe_relative(run_dir), "provider": "deepseek",
                         "model": "deepseek-flash", "revisions": revisions},
        "historical_runs": [
            {"path": f"runs/{name}", "provider": provider}
            for name, provider in HISTORICAL_RUNS.items() if ROOT / "runs" / name != run_dir
        ],
        "history_bundle": "history.bundle" if bundle else None,
        "files": entries,
        "manifest_note": "DELIVERY_MANIFEST.json is excluded from its own file hash list.",
    }
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as archive:
        for name, path in sorted(files.items()):
            archive.write(path, name)
        if bundle:
            archive.write(bundle, "history.bundle")
        archive.writestr("DELIVERY_MANIFEST.json",
                         json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"Delivery archive: {output}")
    print(f"Selected run: {safe_relative(run_dir)} (v1/v2 validated)")
    print(f"Files: {len(entries)}")
    if bundle:
        print(f"Git history bundle: {bundle}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"DELIVERY_PACKAGE_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
