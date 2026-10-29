"""Shared, resumable Responses API agent loop.

The core owns model calls, tool dispatch, checkpoints, and audit events. Product
specific tools and the acceptance rule for ``finish`` belong to the caller.
"""

from __future__ import annotations

import hashlib
import base64
import binascii
import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from openai import OpenAI
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class Config(BaseModel):
    """Configuration shared by coding and media agents.

    Extra fields are retained so product agents can extend their configuration
    without weakening the mandatory model and reasoning constraints.
    """

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    model: str = "deepseek-flash"
    reasoning_effort: str = "high"
    api_base: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    api_retries: int = Field(default=2, ge=0, le=2)
    max_turns: int = Field(default=60, ge=1)
    request_timeout_s: float = Field(default=120, gt=0)
    command_timeout_s: float = Field(default=120, gt=0)
    total_timeout_s: float = Field(default=1800, gt=0)
    repair_rounds: int = Field(default=2, ge=0, le=2)
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    font_path: str | None = None
    output: dict[str, Any] = Field(
        default_factory=lambda: {"width": 1280, "height": 720, "fps": 30}
    )
    pricing: dict[str, Any] = Field(
        default_factory=lambda: {
            "input_per_million": None,
            "cached_input_per_million": None,
            "output_per_million": None,
        }
    )

    @field_validator("model")
    @classmethod
    def _fixed_model(cls, value: str) -> str:
        if value != "deepseek-flash":
            raise ValueError("Project runtime is restricted to deepseek-flash by the user's latest instruction")
        return value

    @field_validator("api_key_env")
    @classmethod
    def _fixed_credential(cls, value: str) -> str:
        if value != "DEEPSEEK_API_KEY":
            raise ValueError("Only DEEPSEEK_API_KEY is authorized; credential fallback is disabled")
        return value

    @field_validator("api_base")
    @classmethod
    def _deepseek_endpoint(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.netloc != "api.deepseek.com" or parts.path.rstrip("/") not in {"", "/v1"} or parts.query or parts.fragment:
            raise ValueError("DEEPSEEK_API_KEY may only be sent to the official https://api.deepseek.com endpoint")
        return value.rstrip("/")

    @field_validator("reasoning_effort")
    @classmethod
    def _fixed_effort(cls, value: str) -> str:
        if value != "high":
            raise ValueError("Only high reasoning effort is authorized")
        return value


def load_config(path: str | Path | None = None) -> Config:
    """Load a JSON configuration file, or defaults when no path is supplied."""
    if path is None:
        return Config()
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a JSON object")
    return Config.model_validate(data)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class EventLogger:
    """Append structured events and write small, atomic JSON state files."""

    def __init__(
        self,
        run_dir: str | Path,
        stage: str,
        request_id: str | None = None,
        revision: str | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.stage = stage
        self.request_id = request_id
        self.revision = revision
        self.events_path = self.run_dir / "events.jsonl"
        self.run_id = uuid.uuid4().hex
        if self.events_path.exists():
            with self.events_path.open("r", encoding="utf-8") as stream:
                first = stream.readline()
            try:
                self.run_id = json.loads(first)["run_id"]
            except (ValueError, KeyError, TypeError):
                pass
        self._secrets = tuple(
            value for name, value in os.environ.items()
            if value and len(value) >= 8 and re.search(
                r"(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", name, re.I
            )
        )

    def sanitize(self, value: Any) -> Any:
        """Remove known secret values and image payloads before persistence."""
        value = _jsonable(value)
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if re.fullmatch(
                    r"(?:api[_-]?key|secret|client[_-]?secret|private[_-]?key|"
                    r"password|authorization|token|access[_-]?token|refresh[_-]?token|"
                    r"bearer[_-]?token|auth[_-]?token)", key, re.I
                ):
                    result[key] = "[REDACTED]"
                elif key in {"image_base64", "b64_json", "base64"} and (
                    not isinstance(item, dict) or "__image_sidecar__" not in item
                ):
                    result[key] = "[IMAGE DATA OMITTED]"
                else:
                    result[key] = self.sanitize(item)
            return result
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        if isinstance(value, str):
            if value.lstrip().startswith(("{", "[")):
                try:
                    nested = json.loads(value)
                except ValueError:
                    pass
                else:
                    if isinstance(nested, (dict, list)):
                        return json.dumps(self.sanitize(nested), ensure_ascii=False)
            value = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "[IMAGE DATA OMITTED]", value)
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        return value

    def event(self, event_type: str, **data: Any) -> str:
        event_id = uuid.uuid4().hex
        if "call_id" in data and "tool_call_id" not in data:
            data["tool_call_id"] = data["call_id"]
        record = {
            "timestamp": _utc_now(),
            "run_id": self.run_id,
            "stage": self.stage,
            "event_id": event_id,
            "parent_event_id": data.pop("parent_event_id", None),
            "request_id": self.request_id,
            "revision": self.revision,
            "actor": data.pop("actor", "agent"),
            "type": event_type,
            "event_type": event_type,
            **data,
        }
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.sanitize(record), ensure_ascii=False) + "\n")
        return event_id

    def write_json(self, name: str, data: Any) -> Path:
        if Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("JSON filename must be a single basename")
        target = self.run_dir / name
        staging = self.run_dir / f".{name}.{uuid.uuid4().hex}.tmp"
        with staging.open("w", encoding="utf-8") as stream:
            json.dump(self.sanitize(data), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        staging.replace(target)
        return target


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


class ModelClient:
    """One place for API retries, usage accounting, and call audit events."""

    def __init__(self, config: Config, logger: EventLogger) -> None:
        self.config = config
        self.logger = logger
        key = os.environ.get(config.api_key_env)
        if not key:
            raise ValueError(f"Required API credential environment variable is unset: {config.api_key_env}")
        options: dict[str, Any] = {
            "api_key": key,
            "max_retries": 0,
            "timeout": config.request_timeout_s,
        }
        # Always explicit: never allow the SDK to inherit another provider's
        # API key or OPENAI_BASE_URL from the surrounding desktop environment.
        options["base_url"] = config.api_base
        self.client = OpenAI(**options)
        usage_path = logger.run_dir / "usage.json"
        if usage_path.exists():
            with usage_path.open("r", encoding="utf-8") as stream:
                previous = json.load(stream)
            self.calls: list[dict[str, Any]] = previous.get("calls", [])
        else:
            self.calls = []

    def _usage(self, response: Any) -> dict[str, Any]:
        usage = _field(response, "usage")
        if usage is None:
            return {"raw_usage": None, "input_tokens": None, "cached_input_tokens": None,
                    "output_tokens": None, "reasoning_tokens": None, "estimated_cost_usd": None}
        input_tokens = _field(usage, "input_tokens")
        output_tokens = _field(usage, "output_tokens")
        input_details = _field(usage, "input_tokens_details")
        output_details = _field(usage, "output_tokens_details")
        cached = _field(input_details, "cached_tokens", 0) or 0
        reasoning = _field(output_details, "reasoning_tokens", 0) or 0
        prices = self.config.pricing
        input_price = prices.get("input_per_million")
        cached_price = prices.get("cached_input_per_million")
        output_price = prices.get("output_per_million")
        cost = None
        if input_tokens is not None and output_tokens is not None and all(
            price is not None for price in (input_price, cached_price, output_price)
        ):
            cost = ((input_tokens - cached) * input_price + cached * cached_price
                    + output_tokens * output_price) / 1_000_000
        return {
            "raw_usage": _jsonable(usage),
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning,
            "estimated_cost_usd": cost,
        }

    def _record_call(self, call: dict[str, Any]) -> None:
        self.calls.append(self.logger.sanitize(call))
        successes = [entry for entry in self.calls if entry["status"] == "succeeded"]
        fields = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")
        totals = {key: sum(entry.get(key) or 0 for entry in successes) for key in fields}
        known_costs = [entry["estimated_cost_usd"] for entry in successes
                       if entry.get("estimated_cost_usd") is not None]
        totals["estimated_cost_usd"] = (
            sum(known_costs) if len(known_costs) == len(successes) and successes else None
        )
        self.logger.write_json("usage.json", {
            "provider": "deepseek",
            "api_base": self.config.api_base,
            "credential_env": self.config.api_key_env,
            "model": self.config.model,
            "pricing": self.config.pricing,
            "calls": self.calls,
            "totals": totals,
            "cost_status": "known" if totals["estimated_cost_usd"] is not None else "unknown",
        })

    @staticmethod
    def _transient(error: Exception) -> bool:
        if isinstance(error, (APIConnectionError, APITimeoutError, RateLimitError)):
            return True
        if isinstance(error, APIStatusError):
            return error.status_code in {408, 409, 429} or error.status_code >= 500
        return False

    def respond(
        self,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        instructions: str = "",
        purpose: str = "agent_step",
        **extra: Any,
    ) -> Any:
        forbidden = {"model", "reasoning", "input", "tools", "instructions"} & extra.keys()
        if forbidden:
            raise ValueError(f"Reserved Responses arguments cannot be overridden: {sorted(forbidden)}")
        if self.config.model != "deepseek-flash" or self.config.reasoning_effort != "high" or self.config.api_key_env != "DEEPSEEK_API_KEY":
            raise ValueError("Delegated agent model and reasoning constraints were changed")
        args: dict[str, Any] = {
            "model": self.config.model,
            "reasoning": {"effort": self.config.reasoning_effort},
            "input": input_items,
            "tools": tools or [],
            **extra,
        }
        if instructions:
            args["instructions"] = instructions
        for attempt in range(self.config.api_retries + 1):
            started = time.monotonic()
            try:
                response = self.client.responses.create(**args)
            except Exception as error:
                elapsed = time.monotonic() - started
                transient = self._transient(error)
                status_code = getattr(error, "status_code", None)
                record = {
                    "timestamp": _utc_now(), "purpose": purpose, "attempt": attempt + 1,
                    "status": "failed", "provider": "deepseek", "api_base": self.config.api_base,
                    "credential_env": self.config.api_key_env, "model": self.config.model,
                    "latency_s": round(elapsed, 3), "http_status": status_code,
                    "request_id": getattr(error, "request_id", None),
                    "error_type": type(error).__name__, "error": str(error),
                    "retryable": transient,
                    "input_tokens": None, "cached_input_tokens": None,
                    "output_tokens": None, "reasoning_tokens": None,
                    "estimated_cost_usd": None,
                }
                self._record_call(record)
                self.logger.event("api_error", actor="model", **record)
                if not transient or attempt >= self.config.api_retries:
                    raise
                delay = min(2 ** attempt + random.uniform(0, 0.25), 8)
                self.logger.event("api_retry", actor="agent", purpose=purpose,
                                  next_attempt=attempt + 2, delay_s=round(delay, 3))
                time.sleep(delay)
                continue
            elapsed = time.monotonic() - started
            usage = self._usage(response)
            record = {
                "timestamp": _utc_now(), "purpose": purpose, "attempt": attempt + 1,
                "status": "succeeded", "provider": "deepseek", "api_base": self.config.api_base,
                "credential_env": self.config.api_key_env, "model": _field(response, "model", self.config.model),
                "latency_s": round(elapsed, 3), "http_status": 200,
                "request_id": _field(response, "_request_id"),
                "response_id": _field(response, "id"), **usage,
            }
            self._record_call(record)
            self.logger.event("api_call", actor="model", **record)
            return response
        raise RuntimeError("Unreachable retry state")


class AgentLoop:
    """Run a tool using agent; only the caller's accepted finish can succeed."""

    def __init__(
        self,
        config: Config,
        logger: EventLogger,
        tool_specs: list[dict[str, Any]],
        handlers: dict[str, Callable[..., Any]],
    ) -> None:
        if "finish" not in handlers or not any(spec.get("name") == "finish" for spec in tool_specs):
            raise ValueError("A finish tool specification and handler are required")
        self.config = config
        self.logger = logger
        self.tool_specs = tool_specs
        self.handlers = handlers
        self.model = ModelClient(config, logger)

    def _pack_images(self, value: Any, key: str = "") -> Any:
        """Keep vision bytes in binary sidecars, outside JSON logs."""
        if isinstance(value, dict):
            return {name: self._pack_images(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [self._pack_images(item, key) for item in value]
        if not isinstance(value, str):
            return value
        if value.lstrip().startswith(("{", "[")):
            try:
                nested = json.loads(value)
            except ValueError:
                pass
            else:
                if isinstance(nested, (dict, list)):
                    return json.dumps(self._pack_images(nested), ensure_ascii=False)
        match = re.fullmatch(r"(data:image/[^;]+;base64,)([A-Za-z0-9+/=]+)", value)
        if match:
            prefix, encoded = match.groups()
        elif key in {"image_base64", "b64_json", "base64"}:
            prefix, encoded = "", value
        else:
            return value
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return "[INVALID IMAGE DATA]"
        image_dir = self.logger.run_dir / "image_sidecars"
        image_dir.mkdir(exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()
        path = image_dir / f"{digest}.bin"
        if not path.exists():
            with path.open("wb") as stream:
                stream.write(raw)
        return {"__image_sidecar__": digest, "prefix": prefix}

    def _restore_images(self, value: Any) -> Any:
        if isinstance(value, dict):
            digest = value.get("__image_sidecar__")
            if digest is not None:
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("Invalid image sidecar reference")
                path = self.logger.run_dir / "image_sidecars" / f"{digest}.bin"
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError("Image sidecar checksum mismatch")
                return value.get("prefix", "") + base64.b64encode(raw).decode("ascii")
            return {name: self._restore_images(item) for name, item in value.items()}
        if isinstance(value, list):
            return [self._restore_images(item) for item in value]
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                nested = json.loads(value)
            except ValueError:
                pass
            else:
                if isinstance(nested, (dict, list)):
                    return json.dumps(self._restore_images(nested), ensure_ascii=False)
        return value

    def _checkpoint(self, state: dict[str, Any]) -> None:
        self.logger.write_json("checkpoint.json", self._pack_images(state))

    def _tool_result(self, call: dict[str, Any]) -> tuple[dict[str, Any], str]:
        name = call.get("name", "")
        call_id = call.get("call_id")
        parent = call.get("event_id")
        try:
            args = json.loads(call.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be a JSON object")
            handler = self.handlers.get(name)
            if handler is None:
                raise ValueError(f"Unknown tool: {name}")
            value = handler(**args)
            result = _jsonable(value)
            if not isinstance(result, dict):
                result = {"ok": True, "value": result}
        except Exception as error:
            result = {"ok": False, "error_type": type(error).__name__, "error": str(error)}
        result = self.logger.sanitize(result)
        self.logger.event("tool_result", actor="tool", parent_event_id=parent,
                          name=name, call_id=call_id, result=result)
        return result, json.dumps(result, ensure_ascii=False)

    def run(self, task: str, system_prompt: str = "", resume: bool = False,
            followup: str | None = None) -> dict[str, Any]:
        checkpoint_path = self.logger.run_dir / "checkpoint.json"
        runtime_config = {"model": self.config.model, "api_base": self.config.api_base,
                          "credential_env": self.config.api_key_env}
        if resume:
            if not checkpoint_path.exists():
                raise FileNotFoundError(f"No checkpoint in {self.logger.run_dir}")
            with checkpoint_path.open("r", encoding="utf-8") as stream:
                state = self._restore_images(json.load(stream))
            if state.get("runtime_config") != runtime_config:
                raise ValueError("Checkpoint belongs to another or legacy provider; use a new run directory after migration")
            if state.get("task") != self.logger.sanitize(task):
                raise ValueError("Resume task differs from checkpoint task")
            if state.get("system_prompt") != self.logger.sanitize(system_prompt):
                raise ValueError("Resume system prompt differs from checkpoint prompt")
            if state.get("status") == "completed":
                return state["result"]
            state["status"] = "running"
            if followup:
                state["input_items"].append({"role": "user", "content": followup})
                self.logger.event("operator_followup", actor="operator", content=followup)
        else:
            state = {
                "version": 1, "run_id": self.logger.run_id, "status": "running",
                "runtime_config": runtime_config,
                "task": task, "system_prompt": system_prompt,
                "turn": 0, "elapsed_s": 0.0,
                "input_items": [{"role": "user", "content": task}],
                "pending_calls": [], "result": None,
            }
            self.logger.event("agent_started", task=task)
            self._checkpoint(state)
        started = time.monotonic()
        prior_elapsed = float(state.get("elapsed_s", 0.0))

        def elapsed() -> float:
            return prior_elapsed + time.monotonic() - started

        def fail(reason: str) -> dict[str, Any]:
            result = {"status": "failed", "ok": False, "reason": self.logger.sanitize(reason),
                      "turns": state["turn"]}
            state.update(status="failed", result=result, elapsed_s=elapsed())
            self._checkpoint(state)
            self.logger.event("agent_failed", reason=reason, turns=state["turn"])
            return result

        try:
            while True:
                if elapsed() >= self.config.total_timeout_s:
                    return fail("Total time limit reached")
                while state["pending_calls"]:
                    call = state["pending_calls"][0]
                    result, output = self._tool_result(call)
                    state["input_items"].append({
                        "type": "function_call_output", "call_id": call["call_id"],
                        "output": output,
                    })
                    state["pending_calls"].pop(0)
                    state["elapsed_s"] = elapsed()
                    self._checkpoint(state)
                    if call["name"] == "finish" and result.get("ok") is True and result.get("accepted") is True:
                        final = {"status": "completed", "ok": True, "turns": state["turn"],
                                 "finish": result}
                        state.update(status="completed", result=final, elapsed_s=elapsed())
                        self._checkpoint(state)
                        self.logger.event("agent_completed", turns=state["turn"], finish=result)
                        return final
                    if elapsed() >= self.config.total_timeout_s:
                        return fail("Total time limit reached")
                if state["turn"] >= self.config.max_turns:
                    return fail("Model turn limit reached without an accepted finish")
                remaining = self.config.total_timeout_s - elapsed()
                response = self.model.respond(
                    state["input_items"], tools=self.tool_specs,
                    instructions=system_prompt, purpose="agent_turn",
                    timeout=min(self.config.request_timeout_s, remaining),
                )
                state["turn"] += 1
                output_items = [_jsonable(item) for item in _field(response, "output", [])]
                # Responses reasoning items and call IDs must be carried forward verbatim.
                state["input_items"].extend(output_items)
                for item in output_items:
                    if item.get("type") != "function_call":
                        continue
                    event_id = self.logger.event(
                        "tool_call", actor="assistant", name=item.get("name"),
                        call_id=item.get("call_id"), arguments=item.get("arguments"),
                    )
                    state["pending_calls"].append({
                        "name": item.get("name"), "call_id": item.get("call_id"),
                        "arguments": item.get("arguments"), "event_id": event_id,
                    })
                if not state["pending_calls"]:
                    state["input_items"].append({
                        "role": "user",
                        "content": "Use the finish tool to submit the result for validation, or call another available tool. A text answer alone does not complete this task.",
                    })
                    self.logger.event("no_tool_call", actor="assistant", turn=state["turn"])
                state["elapsed_s"] = elapsed()
                self._checkpoint(state)
        except Exception as error:
            return fail(f"{type(error).__name__}: {error}")
