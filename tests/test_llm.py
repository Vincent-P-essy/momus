from __future__ import annotations

import pytest

from momus.errors import BudgetExceededError
from momus.llm import (
    AnthropicLLM,
    Usage,
    UsageTally,
    supports_adaptive_thinking,
    with_rolling_cache_breakpoint,
)


def test_tally_cost_matches_price_table() -> None:
    tally = UsageTally()
    tally.add(
        "claude-opus-4-8",
        Usage(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        ),
    )
    # 5 (input) + 25 (output) + 0.5 (cache read @0.1x) + 6.25 (cache write @1.25x)
    assert tally.cost_usd() == pytest.approx(36.75)
    assert tally.calls == 1


def test_tally_unknown_model_yields_no_cost() -> None:
    tally = UsageTally()
    tally.add("claude-experimental-9", Usage(input_tokens=10))
    assert tally.cost_usd() is None


def test_tally_totals_aggregate_across_models() -> None:
    tally = UsageTally()
    tally.add("claude-opus-4-8", Usage(input_tokens=100, output_tokens=10))
    tally.add("claude-haiku-4-5", Usage(input_tokens=50, output_tokens=5))
    totals = tally.totals()
    assert totals.input_tokens == 150
    assert totals.output_tokens == 15


def test_rolling_breakpoint_moves_to_last_block() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "one"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "two"},
            ],
        },
    ]
    marked = with_rolling_cache_breakpoint(messages)
    assert "cache_control" not in marked[0]["content"][0]
    assert marked[1]["content"][0].get("cache_control") is None
    assert marked[1]["content"][1]["cache_control"] == {"type": "ephemeral"}
    # The original is untouched.
    assert "cache_control" in messages[0]["content"][0]
    assert "cache_control" not in messages[1]["content"][1]


def test_rolling_breakpoint_skips_unmarkable_blocks() -> None:
    messages = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]}]
    marked = with_rolling_cache_breakpoint(messages)
    assert "cache_control" not in marked[0]["content"][0]


def test_supports_adaptive_thinking() -> None:
    assert supports_adaptive_thinking("claude-opus-4-8")
    assert supports_adaptive_thinking("claude-sonnet-5")
    assert supports_adaptive_thinking("claude-fable-5")
    assert not supports_adaptive_thinking("claude-haiku-4-5")


def test_budget_guard_blocks_before_the_call() -> None:
    tally = UsageTally()
    tally.add("claude-opus-4-8", Usage(input_tokens=10_000_000))  # ≈ $50 spent
    llm = AnthropicLLM(tally=tally, budget_usd=1.0, api_key="test-key")
    from momus.llm import LLMRequest

    with pytest.raises(BudgetExceededError, match="budget"):
        llm.complete(LLMRequest(model="claude-opus-4-8", system="s", messages=[]))
