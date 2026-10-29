"""Collect factual delivery status, elapsed time and deduplicated API usage."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    reports = ROOT / "reports"
    session = read(reports / "project_session.json")
    now = datetime.now(timezone.utc)
    start = datetime.fromisoformat(session["implementation_started_at"])
    calls = {}
    usage_paths = list((ROOT / "runs").rglob("usage.json")) + list((ROOT / "reports").glob("model_probe_*/usage.json"))
    for path in usage_paths:
        if ".local" in path.parts:
            continue
        for row in read(path).get("calls", []):
            key = row.get("response_id") or row.get("request_id") or json.dumps(
                {k: row.get(k) for k in ("timestamp", "purpose", "attempt", "status", "error_type")}, sort_keys=True)
            calls[key] = {**row, "evidence": str(path.relative_to(ROOT))}
    rows = list(calls.values())
    extra = read(reports / "additional_api_probes.json")["calls"]
    preflight = read(reports / "env_check.json")["model_check"]
    extra.append({"purpose": "Final model-image-tool preflight", "status": "succeeded" if preflight.get("ok") else "failed",
                  "usage": preflight.get("usage"), "cost": None, "evidence": "reports/env_check.json"})
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    for r in rows:
        for k in totals:
            totals[k] += r.get(k) or 0
    for r in extra:
        u = r.get("usage") or {}
        totals["input_tokens"] += u.get("input_tokens") or 0
        totals["output_tokens"] += u.get("output_tokens") or 0
        totals["cached_input_tokens"] += u.get("cached_input_tokens", (u.get("input_tokens_details") or {}).get("cached_tokens", 0)) or 0
        totals["reasoning_tokens"] += u.get("reasoning_tokens", (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)) or 0
    by_provider = {}
    for r in rows:
        provider = r.get("provider") or ("openai_historical" if r.get("model", "").startswith("gpt-") else "unknown")
        entry = by_provider.setdefault(provider, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": None})
        entry["calls"] += 1
        entry["input_tokens"] += r.get("input_tokens") or 0
        entry["output_tokens"] += r.get("output_tokens") or 0
    config = read(ROOT / "config.example.json")
    usage = {"current_runtime": {"model": config["model"], "api_base": config["api_base"], "credential_env": config["api_key_env"]},
             "by_provider": by_provider, "reasoning_effort": "high", "calls": rows,
             "additional_probes": extra, "deduplicated_calls": len(rows), "known_token_totals": totals,
             "cost": None, "cost_status": "unknown", "currency": "USD",
             "unaccounted": ["Unverified provider unit prices", "Usage and billing for failed requests without provider usage",
                             "Codex main chat and collaboration-agent consumption not exposed as API usage here"],
             "note": "Cached input is a subset of input tokens. Reasoning is a subset of output. Do not sum them twice."}
    (reports / "project_usage.json").write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")
    status = {}
    for v in (1, 2):
        p = ROOT / "runs" / "demo" / f"validation_v{v}.json"
        movie = p.with_name(f"output_v{v}.mp4")
        status[f"v{v}"] = {"status": read(p).get("status") if p.exists() else "missing",
                             "output": str(movie.relative_to(ROOT)), "sha256": sha(movie) if movie.exists() else None}
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    summary = {"recorded_at": now.isoformat(), "implementation_elapsed_minutes": round((now-start).total_seconds()/60, 2),
               "timing_scope": session["timing_scope"], "formal_exam_clock_verified": False,
               "user_budget_minutes": 120, "stage_a_commit": session["stage_a_commit"],
               "current_commit": commit, "revisions": status, "usage_file": "reports/project_usage.json"}
    (reports / "delivery_status.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
