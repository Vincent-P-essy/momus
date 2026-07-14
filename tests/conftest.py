"""Shared fixtures: a scripted LLM, a sample repo, and a matching diff."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from momus.config import MomusConfig
from momus.llm import LLMRequest, LLMTurn, TextBlock, ToolCall, Usage, UsageTally
from momus.models import Finding

# Post-change content of src/app.py — must match SAMPLE_DIFF's new side.
APP_PY = (
    "import os\n"
    "\n"
    'def load(path, encoding="utf-8"):\n'
    "    data = open(path, encoding=encoding).read()\n"
    "    return data\n"
    "\n"
    "def main():\n"
    '    print(load("config.txt"))\n'
)

SAMPLE_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,7 +1,8 @@
 import os

-def load(path):
-    return open(path).read()
+def load(path, encoding="utf-8"):
+    data = open(path, encoding=encoding).read()
+    return data

 def main():
     print(load("config.txt"))
"""


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(APP_PY, encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo project\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def cfg() -> MomusConfig:
    return MomusConfig(max_iterations=6, verifier_max_iterations=4)


@dataclass
class FakeLLM:
    """Scripted stand-in for AnthropicLLM: pops one item per complete() call."""

    script: list[LLMTurn | Exception]
    tally: UsageTally | None = None
    requests: list[LLMRequest] = field(default_factory=list)

    def complete(self, request: LLMRequest) -> LLMTurn:
        self.requests.append(request)
        assert self.script, "FakeLLM script exhausted"
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if self.tally is not None:
            self.tally.add(request.model, item.usage)
        return item


def make_call(name: str, call_id: str = "call_1", **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, input=arguments)


def tool_turn(*calls: ToolCall) -> LLMTurn:
    return LLMTurn(
        content=list(calls),
        raw_content=[
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
        ],
        stop_reason="tool_use",
        usage=Usage(input_tokens=100, output_tokens=10),
    )


def text_turn(text: str) -> LLMTurn:
    return LLMTurn(
        content=[TextBlock(text=text)],
        raw_content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=100, output_tokens=10),
    )


def finding_payload(**overrides: Any) -> dict[str, Any]:
    """A submission-shaped finding dict (all strict-mode fields present)."""
    payload: dict[str, Any] = {
        "path": "src/app.py",
        "side": "RIGHT",
        "line": 4,
        "start_line": None,
        "category": "bug",
        "severity": "major",
        "confidence": "medium",
        "title": "File handle is never closed",
        "body": "open() without a context manager leaks the descriptor if read() raises.",
        "evidence": [
            {
                "kind": "diff",
                "location": "src/app.py:R4",
                "excerpt": "open(path, encoding=encoding).read()",
            }
        ],
        "suggestion": None,
    }
    payload.update(overrides)
    return payload


def make_finding(**overrides: Any) -> Finding:
    return Finding.model_validate(finding_payload(**overrides))
