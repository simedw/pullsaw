"""PullSaw CLI - Split large PRs into stacked PRs."""

import sys

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import executor
from .models import Plan
from .session import Session

console = Console()


def display_plan(plan: Plan) -> None:
    """Display the plan in a nice table format."""
    table = Table(title="Stacked PR Plan", show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=3)
    table.add_column("Title", style="cyan")
    table.add_column("Files", style="green")

    for step in plan.steps:
        patterns = step.all_patterns
        files_str = ", ".join(patterns[:3])
        if len(patterns) > 3:
            files_str += f" (+{len(patterns) - 3} more)"

        table.add_row(str(step.id), step.title, files_str)

    console.print(table)


@click.command()
@click.option("--base", default=None, help="Base branch (default: auto-detect main/master)")
@click.option("--head", default=None, help="Head branch (default: current branch)")
@click.option("--strict", is_flag=True, help="Fail if drift is detected vs original")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--dry-run", is_flag=True, help="Generate plan only, don't execute")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.option(
    "--test-cmd", default=None, help="Override test command (e.g., 'mix test test/agidb/cases')"
)
@click.option(
    "--check-cmd",
    default=None,
    help="Check command before tests (e.g., 'mix compile --warnings-as-errors')",
)
@click.option(
    "--plan", "plan_path", default=None, help="Use existing plan file instead of generating"
)
@click.option("--continue", "continue_run", is_flag=True, help="Continue from current step branch")
@click.option("--skip", is_flag=True, help="Skip the current failed step (use with --continue)")
def main(
    base: str | None,
    head: str | None,
    strict: bool,
    yes: bool,
    dry_run: bool,
    verbose: bool,
    test_cmd: str | None,
    check_cmd: str | None,
    plan_path: str | None,
    continue_run: bool,
    skip: bool,
) -> None:
    """Split a large PR into stacked PRs using Claude Code.

    Analyzes the diff between base and head branches, generates a plan
    for splitting into multiple smaller PRs, and executes each step.
    """
    console.print(Panel.fit("[bold]PullSaw[/] - Stacked PR Splitter", style="blue"))

    # Create session (handles --continue, branch inference, config)
    try:
        session = Session.from_args(
            base=base,
            head=head,
            strict=strict,
            test_cmd=test_cmd,
            check_cmd=check_cmd,
            continue_run=continue_run,
            skip=skip,
            verbose=verbose,
        )
    except ValueError as e:
        console.print(f"[red]Error: {e}[/]")
        sys.exit(1)

    # Setup working directory (before clean check)
    gitignore_modified = session.setup_pullsaw_dir()

    # Check clean working tree (unless --continue)
    try:
        session.ensure_clean_working_tree(allow_gitignore=gitignore_modified)
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/]")
        sys.exit(1)

    # Load changed files
    changed_files = session.load_changed_files()
    if not changed_files:
        console.print("[yellow]No changes detected between branches.[/]")
        sys.exit(0)

    # Print session info
    session.print_info()

    # Load or generate plan
    try:
        plan = session.load_or_generate_plan(plan_path)
    except RuntimeError as e:
        console.print(f"[red]{e}[/]")
        sys.exit(1)

    # Validate plan
    validation_errors = plan.validate(changed_files)
    fatal_errors = [e for e in validation_errors if e.fatal]
    warnings = [e for e in validation_errors if not e.fatal]

    for warning in warnings:
        console.print(f"[yellow]Warning: {warning.message}[/]")

    if fatal_errors:
        console.print("[red]Plan validation failed:[/]")
        for error in fatal_errors:
            console.print(f"  - {error.message}")
        sys.exit(1)

    # Display plan
    console.print()
    display_plan(plan)

    if dry_run:
        console.print("\n[dim]Dry run - not executing[/]")
        sys.exit(0)

    # Confirm
    if not yes:
        console.print()
        if not click.confirm("Proceed with execution?"):
            console.print("[dim]Aborted[/]")
            sys.exit(0)

    # Execute
    console.print("\n[bold]Executing plan...[/]")
    try:
        branches = executor.execute(
            plan,
            session.base,
            session.head,
            session.config,
            start_from=session.start_from,
            skip_current=skip,
        )
    except RuntimeError as e:
        console.print(f"\n[red]Execution failed: {e}[/]")
        sys.exit(1)

    # Summary
    console.print("\n[bold green]Done![/] Created branches:")
    for branch in branches:
        console.print(f"  - {branch}")


if __name__ == "__main__":
    main()
