# Momus evals

A reviewer you can't measure is a reviewer you can't trust. This harness runs
the full pipeline (reviewer **and** verifier, live model) against small
synthetic pull requests with **planted, labelled bugs**, and reports
precision/recall.

## How a case works

```
cases/<name>/
├── base/           # repository state before the change
├── head/           # state the "PR" proposes
└── expected.toml   # what a good reviewer must catch (empty = clean case)
```

The runner builds a real git repo from `base/`, commits, applies `head/`, and
takes the actual `git diff` — so the model reviews exactly what it would see
on a real PR, including full repo context for its tools.

Scoring: a planted finding is **caught** when a published finding lands on the
same file within 3 lines with the expected category. Published findings that
match nothing planted are **false positives** — which is why a third of the
cases are *clean* diffs where the only correct review is silence.

## Running

```sh
export ANTHROPIC_API_KEY=...
uv run python evals/run.py                       # all cases
uv run python evals/run.py --cases mutable-default tz-naive-compare
uv run python evals/run.py --model claude-sonnet-5 --effort medium
uv run python evals/run.py --no-verify           # measure the verifier's contribution
```

Writes `evals/report.md` and `evals/report.json`. Comparing a `--no-verify`
run against a default run shows exactly what the adversarial verification
pass buys (and costs) in precision/recall.

## Honesty section

Eleven cases is a smoke test, not a benchmark: results are directional, single
runs are noisy, and planted bugs are easier than real-world ones because the
diff is small and self-contained. The value is in the *trend* — tracking
precision/recall per prompt version (`momus.prompts.PROMPT_VERSION`) across
changes to the prompts, models, or pipeline. Contributions of harder cases
(especially ones that require cross-file investigation) are very welcome.
