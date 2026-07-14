"""Thin, typed layer over the Anthropic API.

Design goals:
- The agent code never touches SDK objects: requests and turns are plain local
  types, so tests script the model with a fake and stay fully offline.
- Every request is streamed (long outputs must not hit HTTP timeouts).
- Usage is tallied per model and converted to USD; a hard budget aborts the run
  before the call that would exceed it.
- Prompt caching: one breakpoint on the system prompt, one rolling breakpoint
  on the newest message block, so each loop iteration re-reads the previous
  iteration's prefix at cache-read rates.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol

from momus.errors import BudgetExceededError, ModelRefusalError, MomusError

#: USD per million tokens (input, output). Cache reads bill at 0.1x the input
#: rate, cache writes at 1.25x (5-minute TTL).
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25

#: Models that accept `thinking: {"type": "adaptive"}` and `output_config.effort`.
_ADAPTIVE_PREFIXES = (
    "claude-fable",
    "claude-mythos",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
)


@dataclass(slots=True)
class TextBlock:
    text: str


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(slots=True)
class LLMTurn:
    content: list[TextBlock | ToolCall]
    raw_content: list[dict[str, Any]]
    stop_reason: str
    usage: Usage

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [block for block in self.content if isinstance(block, ToolCall)]

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.content if isinstance(block, TextBlock))


@dataclass(slots=True)
class LLMRequest:
    model: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: dict[str, Any] | None = None
    max_tokens: int = 16_000
    effort: str | None = None


class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMTurn: ...


class UsageTally:
    """Token usage and derived cost across every call of a review run."""

    def __init__(self) -> None:
        self.calls = 0
        self.per_model: dict[str, Usage] = {}

    def add(self, model: str, usage: Usage) -> None:
        self.calls += 1
        agg = self.per_model.setdefault(model, Usage())
        agg.input_tokens += usage.input_tokens
        agg.output_tokens += usage.output_tokens
        agg.cache_read_tokens += usage.cache_read_tokens
        agg.cache_write_tokens += usage.cache_write_tokens

    def totals(self) -> Usage:
        total = Usage()
        for usage in self.per_model.values():
            total.input_tokens += usage.input_tokens
            total.output_tokens += usage.output_tokens
            total.cache_read_tokens += usage.cache_read_tokens
            total.cache_write_tokens += usage.cache_write_tokens
        return total

    def cost_usd(self) -> float | None:
        """Total cost, or None when a model without a known price was used."""
        total = 0.0
        for model, usage in self.per_model.items():
            prices = MODEL_PRICES.get(model)
            if prices is None:
                return None
            price_in, price_out = prices
            total += usage.input_tokens * price_in / 1e6
            total += usage.cache_write_tokens * price_in * _CACHE_WRITE_MULT / 1e6
            total += usage.cache_read_tokens * price_in * _CACHE_READ_MULT / 1e6
            total += usage.output_tokens * price_out / 1e6
        return total


def with_rolling_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy messages with exactly one cache breakpoint, on the newest block."""
    result = copy.deepcopy(messages)
    for message in result:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    if result:
        content = result[-1].get("content")
        if isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict) and last.get("type") in {"text", "tool_result"}:
                last["cache_control"] = {"type": "ephemeral"}
    return result


def supports_adaptive_thinking(model: str) -> bool:
    return model.startswith(_ADAPTIVE_PREFIXES)


class AnthropicLLM:
    """Production client. Requires ANTHROPIC_API_KEY (or an `ant auth` profile)."""

    def __init__(
        self,
        tally: UsageTally,
        budget_usd: float | None = None,
        api_key: str | None = None,
    ) -> None:
        import anthropic

        self._anthropic = anthropic
        client_kwargs: dict[str, Any] = {"max_retries": 3}
        if api_key:
            client_kwargs["api_key"] = api_key
        self._client = anthropic.Anthropic(**client_kwargs)
        self.tally = tally
        self.budget_usd = budget_usd

    def complete(self, request: LLMRequest) -> LLMTurn:
        self._check_budget()
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": request.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": request.messages,
        }
        if request.tools:
            kwargs["tools"] = request.tools
        if request.tool_choice is not None:
            # Forced tool choice: skip thinking so the answer is the tool call.
            kwargs["tool_choice"] = request.tool_choice
        elif supports_adaptive_thinking(request.model):
            kwargs["thinking"] = {"type": "adaptive"}
        if request.effort and supports_adaptive_thinking(request.model):
            kwargs["output_config"] = {"effort": request.effort}

        try:
            with self._client.messages.stream(**kwargs) as stream:
                message: Any = stream.get_final_message()
        except self._anthropic.APIStatusError as exc:  # pragma: no cover - network path
            raise MomusError(f"Anthropic API error: {exc}") from exc
        except self._anthropic.APIConnectionError as exc:  # pragma: no cover - network path
            raise MomusError(f"could not reach the Anthropic API: {exc}") from exc

        stop_reason = str(message.stop_reason or "")
        if stop_reason == "refusal":
            raise ModelRefusalError(
                "the model declined this request (stop_reason=refusal); "
                "try a different model via --model"
            )
        if stop_reason == "model_context_window_exceeded":
            raise MomusError(
                "the diff plus gathered context exceeded the model's context window; "
                "narrow the review with `ignore` globs or a smaller PR"
            )

        turn = LLMTurn(
            content=_parse_blocks(message.content),
            raw_content=[_dump_block(block) for block in message.content],
            stop_reason=stop_reason,
            usage=_parse_usage(message.usage),
        )
        self.tally.add(request.model, turn.usage)
        return turn

    def _check_budget(self) -> None:
        if self.budget_usd is None:
            return
        cost = self.tally.cost_usd()
        if cost is not None and cost >= self.budget_usd:
            raise BudgetExceededError(
                f"review budget of ${self.budget_usd:.2f} reached "
                f"(spent ${cost:.2f} over {self.tally.calls} calls)"
            )


def _parse_blocks(content: Any) -> list[TextBlock | ToolCall]:
    blocks: list[TextBlock | ToolCall] = []
    for block in content:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            blocks.append(TextBlock(text=str(block.text)))
        elif block_type == "tool_use":
            raw_input = block.input
            tool_input = dict(raw_input) if isinstance(raw_input, dict) else {}
            blocks.append(ToolCall(id=str(block.id), name=str(block.name), input=tool_input))
        # thinking / redacted_thinking blocks are preserved in raw_content only.
    return blocks


def _dump_block(block: Any) -> dict[str, Any]:
    if hasattr(block, "model_dump"):
        dumped = block.model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    return dict(block)


def _parse_usage(usage: Any) -> Usage:
    def _get(name: str) -> int:
        value = getattr(usage, name, 0)
        return int(value) if value else 0

    return Usage(
        input_tokens=_get("input_tokens"),
        output_tokens=_get("output_tokens"),
        cache_read_tokens=_get("cache_read_input_tokens"),
        cache_write_tokens=_get("cache_creation_input_tokens"),
    )
