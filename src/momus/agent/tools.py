"""Tool definitions exposed to the model, and their dispatch.

All schemas use strict mode: every property is required (optionals are
nullable), objects forbid additional properties, so tool inputs arrive exactly
as typed and the repair path only ever deals with semantic errors.
"""

from __future__ import annotations

from typing import Any

from momus.context import RepoContext
from momus.llm import ToolCall
from momus.models import Category, Confidence, EvidenceKind, Severity, Side, Verdict

SUBMIT_FINDINGS = "submit_findings"
SUBMIT_VERDICT = "submit_verdict"


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _enum(values: type) -> dict[str, Any]:
    return {"type": "string", "enum": [member.value for member in values]}  # type: ignore[attr-defined]


def context_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "read_file",
            "description": (
                "Read a file from the repository (post-change state), with line numbers. "
                "Call this before commenting on any code, to see the full surrounding "
                "function or class. Use start_line/end_line to window large files."
            ),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repository-relative path."},
                    "start_line": _nullable({"type": "integer"}),
                    "end_line": _nullable({"type": "integer"}),
                },
                "required": ["path", "start_line", "end_line"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search",
            "description": (
                "Search file contents across the repository with a regular expression. "
                "Call this to find callers of a changed symbol, existing conventions, or "
                "proof for any claim about the rest of the codebase. Returns "
                "path:line:text matches."
            ),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression."},
                    "glob": _nullable(
                        {"type": "string", "description": "Limit to files matching this glob."}
                    ),
                },
                "required": ["pattern", "glob"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_dir",
            "description": "List one directory of the repository (directories end with /).",
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repository-relative directory."}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    ]


def _finding_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "side": _enum(Side),
            "line": {"type": "integer"},
            "start_line": _nullable({"type": "integer"}),
            "category": _enum(Category),
            "severity": _enum(Severity),
            "confidence": _enum(Confidence),
            "title": {"type": "string"},
            "body": {"type": "string"},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": _enum(EvidenceKind),
                        "location": {"type": "string"},
                        "excerpt": {"type": "string"},
                    },
                    "required": ["kind", "location", "excerpt"],
                    "additionalProperties": False,
                },
            },
            "suggestion": _nullable({"type": "string"}),
        },
        "required": [
            "path",
            "side",
            "line",
            "start_line",
            "category",
            "severity",
            "confidence",
            "title",
            "body",
            "evidence",
            "suggestion",
        ],
        "additionalProperties": False,
    }


def submit_findings_tool() -> dict[str, Any]:
    return {
        "name": SUBMIT_FINDINGS,
        "description": (
            "Deliver the completed review. Call exactly once, after the investigation, "
            "with every finding and an overall summary."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "findings": {"type": "array", "items": _finding_schema()},
            },
            "required": ["summary", "findings"],
            "additionalProperties": False,
        },
    }


def submit_verdict_tool() -> dict[str, Any]:
    return {
        "name": SUBMIT_VERDICT,
        "description": "Deliver the verdict on the draft comment. Call exactly once.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": _enum(Verdict),
                "rebuttal": {"type": "string"},
                "revised_severity": _nullable(_enum(Severity)),
                "revised_confidence": _nullable(_enum(Confidence)),
                "revised_body": _nullable({"type": "string"}),
            },
            "required": [
                "verdict",
                "rebuttal",
                "revised_severity",
                "revised_confidence",
                "revised_body",
            ],
            "additionalProperties": False,
        },
    }


def run_context_tool(ctx: RepoContext, call: ToolCall) -> str:
    """Execute one read-only context tool; errors come back as strings."""
    args = call.input
    if call.name == "read_file":
        return ctx.read_file(
            str(args.get("path", "")),
            _int_or_none(args.get("start_line")),
            _int_or_none(args.get("end_line")),
        )
    if call.name == "search":
        glob = args.get("glob")
        return ctx.search(str(args.get("pattern", "")), str(glob) if glob else None)
    if call.name == "list_dir":
        return ctx.list_dir(str(args.get("path") or "."))
    return f"error: unknown tool {call.name!r}"


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
