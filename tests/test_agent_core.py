"""Responses loop tests using a scripted client, with no API traffic."""

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from pydantic import ValidationError

from agent_core import AgentLoop, Config, EventLogger


TEST_ROOT = Path(__file__).resolve().parents[1] / ".local" / "tests"


def function_call(name, call_id, arguments):
    return {"type": "function_call", "name": name, "call_id": call_id, "arguments": arguments}


class ScriptedResponses:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return {
            "id": f"response-{len(self.requests)}",
            "model": "gpt-6-sol",
            "output": next(self.outputs),
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


class AgentCoreTests(unittest.TestCase):
    def setUp(self):
        self.run_dir = TEST_ROOT / f"{self._testMethodName}-{uuid.uuid4().hex}"
        self.logger = EventLogger(self.run_dir, stage="test")
        self.specs = [
            {"type": "function", "name": "finish", "parameters": {"type": "object"}},
            {"type": "function", "name": "work", "parameters": {"type": "object"}},
        ]

    def run_script(self, outputs, handlers, max_turns=4):
        scripted = ScriptedResponses(outputs)
        fake_client = SimpleNamespace(responses=scripted)
        config = Config(api_key_env="UNIT_TEST_OPENAI_KEY", max_turns=max_turns, api_retries=0)
        with patch.dict(os.environ, {"UNIT_TEST_OPENAI_KEY": "not-a-real-key"}), \
             patch("agent_core.OpenAI", return_value=fake_client) as constructor:
            result = AgentLoop(config, self.logger, self.specs, handlers).run("task", "rules")
        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
        for request in scripted.requests:
            self.assertEqual(request["model"], "gpt-6-sol")
            self.assertEqual(request["reasoning"], {"effort": "high"})
        return result, scripted.requests

    def test_rejected_finish_continues_and_preserves_reasoning_and_tool_output(self):
        decisions = iter([{"ok": False, "accepted": False, "error": "test failed"},
                          {"ok": True, "accepted": True, "summary": "fixed"}])
        reasoning = {"type": "reasoning", "id": "reason-1", "summary": [{"type": "summary_text", "text": "try repair"}]}
        outputs = [
            [reasoning, function_call("finish", "call-1", '{"summary":"first"}')],
            [function_call("finish", "call-2", '{"summary":"second"}')],
        ]
        result, requests = self.run_script(outputs, {"finish": lambda summary: next(decisions)})
        self.assertTrue(result["ok"])
        self.assertEqual(result["turns"], 2)
        self.assertEqual(requests[1]["input"][1], reasoning)
        outputs_seen = [x for x in requests[1]["input"] if x.get("type") == "function_call_output"]
        self.assertEqual(outputs_seen[0]["call_id"], "call-1")
        self.assertEqual(json.loads(outputs_seen[0]["output"])["accepted"], False)
        checkpoint = json.loads((self.run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["status"], "completed")

    def test_malformed_arguments_and_handler_exception_become_tool_errors(self):
        outputs = [
            [function_call("work", "bad-json", "{"),
             function_call("work", "raised", '{"action":"boom"}')],
            [function_call("finish", "done", "{}")],
        ]

        def work(action):
            raise RuntimeError("deliberate handler failure")

        result, requests = self.run_script(
            outputs,
            {"work": work, "finish": lambda: {"ok": True, "accepted": True}},
        )
        self.assertTrue(result["ok"])
        errors = {x["call_id"]: json.loads(x["output"])
                  for x in requests[1]["input"] if x.get("type") == "function_call_output"}
        self.assertEqual(errors["bad-json"]["error_type"], "JSONDecodeError")
        self.assertFalse(errors["bad-json"]["ok"])
        self.assertEqual(errors["raised"]["error_type"], "RuntimeError")
        self.assertIn("deliberate handler failure", errors["raised"]["error"])

    def test_plain_model_text_cannot_complete_and_budget_saves_failure(self):
        outputs = [[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done"}]}]]
        result, requests = self.run_script(outputs, {"finish": lambda: {"ok": True, "accepted": True}}, max_turns=1)
        self.assertFalse(result["ok"])
        self.assertIn("turn limit", result["reason"])
        self.assertEqual(len(requests), 1)
        checkpoint = json.loads((self.run_dir / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["status"], "failed")
        self.assertEqual(checkpoint["turn"], 1)
        self.assertEqual(checkpoint["result"], result)
        self.assertTrue(any(x.get("type") == "message" for x in checkpoint["input_items"]))

    def test_model_and_reasoning_restrictions(self):
        with self.assertRaises(ValidationError):
            Config(model="gpt-6-luna")
        with self.assertRaises(ValidationError):
            Config(reasoning_effort="medium")
        self.assertEqual((Config().model, Config().reasoning_effort), ("gpt-6-sol", "high"))


if __name__ == "__main__":
    unittest.main()
