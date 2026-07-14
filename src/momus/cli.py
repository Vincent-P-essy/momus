"""Command-line interface."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from momus import __version__
from momus.config import MomusConfig
from momus.errors import MomusError
from momus.github import GitHubClient
from momus.models import SEVERITY_RANK, ReviewResult, Severity
from momus.pipeline import local_diff, run_review
from momus.publish import publish_to_github, render_to_terminal


@click.group(help="Momus — an AI code reviewer that only posts what survives cross-examination.")
@click.version_option(__version__, prog_name="momus")
def main() -> None: ...


@main.command(help="Review a GitHub pull request, or the local diff with --local.")
@click.option("--repo", "repo_slug", metavar="OWNER/NAME", help="GitHub repository.")
@click.option("--pr", "pr_number", type=int, metavar="N", help="Pull request number.")
@click.option(
    "--local",
    "local_base",
    is_flag=False,
    flag_value="HEAD",
    default=None,
    metavar="[BASE]",
    help="Review the local diff (working tree vs HEAD, or vs BASE...HEAD).",
)
@click.option(
    "--repo-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    help="Path to the repository checkout to review against.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Config file (default: .momus.toml in the repo root).",
)
@click.option("--model", help="Reviewer model id (default: claude-opus-4-8).")
@click.option("--verifier-model", help="Verifier model id (default: same as --model).")
@click.option("--effort", type=click.Choice(["low", "medium", "high", "xhigh", "max"]))
@click.option("--language", help="Language for review comments (default: en).")
@click.option("--budget", "budget_usd", type=float, help="Hard USD budget for the run.")
@click.option("--max-findings", type=int, help="Cap on posted findings.")
@click.option("--no-verify", is_flag=True, help="Skip the adversarial verification pass.")
@click.option(
    "--include-untracked",
    is_flag=True,
    help="Local mode: include brand-new (untracked) files in the review.",
)
@click.option("--dry-run", is_flag=True, help="Review but do not post to GitHub.")
@click.option("--json", "json_output", is_flag=True, help="Print the result as JSON.")
@click.option(
    "--fail-on",
    type=click.Choice([s.value for s in Severity]),
    help="Exit with status 2 if any finding is at or above this severity.",
)
def review(
    repo_slug: str | None,
    pr_number: int | None,
    local_base: str | None,
    repo_root: Path,
    config_path: Path | None,
    model: str | None,
    verifier_model: str | None,
    effort: str | None,
    language: str | None,
    budget_usd: float | None,
    max_findings: int | None,
    no_verify: bool,
    include_untracked: bool,
    dry_run: bool,
    json_output: bool,
    fail_on: str | None,
) -> None:
    if local_base is None and not (repo_slug and pr_number):
        raise click.UsageError("provide --repo OWNER/NAME and --pr N, or use --local")

    root = repo_root.resolve()
    try:
        cfg = MomusConfig.load(
            root,
            config_path,
            model=model,
            verifier_model=verifier_model,
            effort=effort,
            language=language,
            budget_usd=budget_usd,
            max_findings=max_findings,
            verify=False if no_verify else None,
        )
        result = _run(
            root, cfg, repo_slug, pr_number, local_base, include_untracked, dry_run, json_output
        )
    except MomusError as exc:
        raise click.ClickException(str(exc)) from exc

    if fail_on and result.findings:
        worst = result.worst_severity()
        if worst is not None and SEVERITY_RANK[worst] <= SEVERITY_RANK[Severity(fail_on)]:
            click.echo(f"failing: found {worst.value}-severity issue(s)", err=True)
            sys.exit(2)


def _run(
    root: Path,
    cfg: MomusConfig,
    repo_slug: str | None,
    pr_number: int | None,
    local_base: str | None,
    include_untracked: bool,
    dry_run: bool,
    json_output: bool,
) -> ReviewResult:
    if local_base is not None:
        pr, diff_text = local_diff(
            root,
            None if local_base == "HEAD" else local_base,
            include_untracked=include_untracked,
        )
        result = run_review(root, pr, diff_text, cfg)
        _emit(result, json_output)
        return result

    assert repo_slug is not None and pr_number is not None
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise MomusError("GITHUB_TOKEN (or GH_TOKEN) is required to review a pull request")
    gh = GitHubClient(token, repo_slug)
    pr = gh.get_pr(pr_number)
    if pr.draft:
        click.echo("draft pull request — skipping review")
        return ReviewResult(summary="draft pull request — skipped")
    diff_text = gh.get_pr_diff(pr_number)
    result = run_review(root, pr, diff_text, cfg)

    if dry_run or json_output:
        _emit(result, json_output)
    else:
        click.echo(publish_to_github(gh, pr, result, cfg))
    return result


def _emit(result: ReviewResult, json_output: bool) -> None:
    if json_output:
        click.echo(result.model_dump_json(indent=2))
    else:
        render_to_terminal(result)


if __name__ == "__main__":
    main()
