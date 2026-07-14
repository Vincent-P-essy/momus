"""Render a ReviewResult in the terminal (local mode / --dry-run)."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from momus.models import Finding, ReviewResult, Severity

_SEVERITY_STYLE = {
    Severity.BLOCKER: "bold red",
    Severity.MAJOR: "bold dark_orange",
    Severity.MINOR: "bold yellow",
    Severity.NIT: "bold bright_black",
}


def _print_finding(console: Console, finding: Finding) -> None:
    style = _SEVERITY_STYLE[finding.severity]
    console.print(
        Text.assemble(
            (f"[{finding.severity.value}] ", style),
            (finding.title, "bold"),
            (f"  ({finding.category.value}, confidence {finding.confidence.value})", "dim"),
        )
    )
    console.print(f"  [cyan]{finding.path}[/]:{finding.line} ({finding.side.value})")
    for line in finding.body.strip().splitlines():
        console.print(f"  {line}")
    for evidence in finding.evidence:
        console.print(f"  [dim]evidence · {evidence.kind.value} · {evidence.location}[/]")
    if finding.suggestion is not None and finding.suggestion.strip():
        console.print("  [green]suggested replacement:[/]")
        for line in finding.suggestion.rstrip("\n").splitlines():
            console.print(f"  [green]| {line}[/]")
    console.print()


def render_to_terminal(result: ReviewResult, console: Console | None = None) -> None:
    console = console or Console()
    console.print(Panel(result.summary.strip(), title="Momus review", border_style="magenta"))
    console.print()

    if not result.findings:
        console.print("[bold green]No findings survived verification.[/]\n")
    for finding in sorted(result.findings, key=Finding.sort_key):
        _print_finding(console, finding)

    if result.unanchored:
        console.print("[bold]Not anchorable to the diff (summary-only):[/]")
        for finding in result.unanchored:
            console.print(f"  - {finding.title} ({finding.path}:{finding.line})")
        console.print()

    if result.rejected:
        console.print("[bold]Rejected during verification:[/]")
        for rejected in result.rejected:
            console.print(f"  [strike]{rejected.finding.title}[/] — [dim]{rejected.rebuttal}[/]")
        console.print()

    stats = result.stats
    cost = f"${stats.cost_usd:.2f}" if stats.cost_usd is not None else "cost n/a"
    console.print(
        f"[dim]{stats.files_reviewed} file(s) (+{stats.additions} -{stats.deletions}) · "
        f"{stats.published_findings} finding(s), {stats.rejected_findings} rejected · "
        f"{stats.model_calls} model calls · {cost} · {stats.duration_seconds:.0f}s[/]"
    )
