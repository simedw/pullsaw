"""PullSaw CLI - Split large PRs into stacked PRs."""

import sys
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import claude_code, executor, git_ops
from .config import Config

console = Console()


def display_plan(plan: dict) -> None:
    """Display the plan in a nice table format."""
    table = Table(title="Stacked PR Plan", show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=3)
    table.add_column("Title", style="cyan")
    table.add_column("Files", style="green")

    for step in plan.get("stack", []):
        step_id = str(step.get("id", "?"))
        title = step.get("title", "No title")
        patterns = step.get("allow", []) + step.get("shared_allow", [])
        files_str = ", ".join(patterns[:3])
        if len(patterns) > 3:
            files_str += f" (+{len(patterns) - 3} more)"

        table.add_row(step_id, title, files_str)

    console.print(table)


@click.command()
@click.option("--base", default=None, help="Base branch (default: auto-detect main/master)")
@click.option("--head", default=None, help="Head branch (default: current branch)")
@click.option("--strict", is_flag=True, help="Fail if drift is detected vs original")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--dry-run", is_flag=True, help="Generate plan only, don't execute")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.option("--test-cmd", default=None, help="Override test command (e.g., 'mix test test/agidb/cases')")
@click.option("--check-cmd", default=None, help="Check command before tests (e.g., 'mix compile --warnings-as-errors')")
@click.option("--plan", "plan_path", default=None, help="Use existing plan file instead of generating")
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

    # Get repo root early (needed for .pullsaw setup)
    repo_root = git_ops.get_repo_root()

    # Handle --continue: detect step from current branch
    start_from = 1
    if continue_run:
        import re
        current = git_ops.current_branch()
        match = re.match(r"^(.+)-step-(\d+)$", current)
        if not match:
            console.print(f"[red]Error: Current branch '{current}' doesn't match pattern '{{head}}-step-N'[/]")
            console.print("[dim]Run --continue from a step branch (e.g., my-feature-step-3)[/]")
            sys.exit(1)

        derived_head = match.group(1)
        start_from = int(match.group(2))

        # Auto-set head and plan path
        if head is None:
            head = derived_head
        plan_file_path = claude_code.get_plan_file(str(repo_root), derived_head)
        if plan_path is None:
            plan_path = plan_file_path

        action = "Skipping" if skip else "Retrying"
        console.print(f"[cyan]Continuing from step {start_from}[/] ({action})")
        console.print(f"[dim]Original head: {derived_head}[/]")

    # Setup .pullsaw directory and .gitignore BEFORE clean check
    pullsaw_dir = repo_root / claude_code.SKIKT_DIR
    pullsaw_dir.mkdir(exist_ok=True)

    gitignore = repo_root / ".gitignore"
    gitignore_entry = f"/{claude_code.SKIKT_DIR}/"
    gitignore_modified = False
    if gitignore.exists():
        content = gitignore.read_text()
        if gitignore_entry not in content and claude_code.SKIKT_DIR not in content:
            with open(gitignore, "a") as f:
                f.write(f"\n# PullSaw working directory\n{gitignore_entry}\n")
            console.print(f"[dim]Added {gitignore_entry} to .gitignore[/]")
            gitignore_modified = True
    else:
        gitignore.write_text(f"# PullSaw working directory\n{gitignore_entry}\n")
        console.print(f"[dim]Created .gitignore with {gitignore_entry}[/]")
        gitignore_modified = True

    # Check for clean working tree (skip when continuing - we expect uncommitted changes)
    if continue_run:
        changed = git_ops.changed_files_working()
        console.print(f"Working tree: [yellow]{len(changed)} uncommitted files[/] (continuing)")
    else:
        console.print("Checking working tree...", end=" ")
        if not git_ops.is_clean():
            # Check if only .gitignore was changed (by us)
            changed = git_ops.changed_files_working()
            only_gitignore = gitignore_modified and set(changed.keys()) == {".gitignore"}
            if not only_gitignore:
                console.print("[red]dirty[/]")
                console.print("[red]Error: Working tree has uncommitted changes. Commit or stash first.[/]")
                sys.exit(1)
        console.print("[green]clean[/]")

    # Infer or use provided branches
    try:
        if base is None or head is None:
            inferred_base, inferred_head = git_ops.infer_branches()
            base = base or inferred_base
            head = head or inferred_head
    except ValueError as e:
        console.print(f"[red]Error: {e}[/]")
        sys.exit(1)

    # Use merge-base to get only changes in the feature branch
    # (not changes in main since branching)
    merge_base_ref = git_ops.merge_base(base, head)

    console.print(f"Base: [cyan]{base}[/]")
    console.print(f"Head: [cyan]{head}[/]")
    if verbose:
        console.print(f"Merge base: [dim]{merge_base_ref[:12]}[/]")

    # Get changed files (from merge-base, not from base tip)
    changed_files = git_ops.changed_files(merge_base_ref, head)
    if not changed_files:
        console.print("[yellow]No changes detected between branches.[/]")
        sys.exit(0)

    console.print(f"Changed files: [green]{len(changed_files)}[/]")

    if verbose:
        for filepath, status in sorted(changed_files.items()):
            console.print(f"  [dim]{status.value} {filepath}[/]")

    # Get diff stats for planning (using merge-base)
    name_status = git_ops.run_git("diff", "--name-status", f"{merge_base_ref}..{head}").stdout
    stat = git_ops.diff_stat(merge_base_ref, head)

    # Load config
    config = Config.load(repo_root)
    config.strict = strict or config.strict

    # Override commands if provided
    if test_cmd:
        config.test_cmd = test_cmd.split()
    if check_cmd:
        config.check_cmd = check_cmd.split()

    console.print(f"Test command: [dim]{' '.join(config.test_cmd)}[/]")
    if config.check_cmd:
        console.print(f"Check command: [dim]{' '.join(config.check_cmd)}[/]")
    if config.format_cmd:
        console.print(f"Format command: [dim]{' '.join(config.format_cmd)}[/]")

    # Use existing plan or generate new one
    if plan_path:
        plan_file = Path(plan_path)
        if not plan_file.exists():
            console.print(f"[red]Plan file not found: {plan_file}[/]")
            sys.exit(1)
        console.print(f"\n[bold]Using existing plan:[/] {plan_file}")
    else:
        # Generate plan
        console.print("\n[bold]Generating plan via Claude Code...[/]")

        plan_file_path = claude_code.get_plan_file(str(repo_root), head)
        plan_file = Path(plan_file_path)

        # Clean up old plan file if exists
        if plan_file.exists():
            plan_file.unlink()

        plan_result = claude_code.generate_plan(base, head, name_status, stat, plan_file_path)

        if not plan_result.success:
            console.print(f"[red]Failed to generate plan: {plan_result.error}[/]")
            if plan_result.output:
                console.print(f"Output: {plan_result.output[:500]}")
            sys.exit(1)

        # Show result info
        if verbose:
            console.print(f"\n[dim]Session: {plan_result.session_id}[/]")
            if plan_result.raw:
                console.print(f"[dim]Turns: {plan_result.raw.get('num_turns')}, Cost: ${plan_result.raw.get('total_cost_usd', 0):.4f}[/]")
        if plan_result.error:
            console.print(f"[yellow]Warning: {plan_result.error}[/]")

    # Read plan from file
    if not plan_file.exists():
        console.print(f"[red]Plan file not found: {plan_file}[/]")
        if not plan_path:  # Only show Claude output if we tried to generate
            console.print(f"[dim]Claude output: {plan_result.output[:1000]}[/]")
        sys.exit(1)

    plan_text = plan_file.read_text()
    if verbose:
        console.print(f"[dim]Plan file: {plan_file} ({len(plan_text)} chars)[/]")

    try:
        plan = yaml.safe_load(plan_text)
    except yaml.YAMLError as e:
        console.print(f"[red]Failed to parse plan YAML: {e}[/]")
        console.print(f"[dim]Plan file contents:\n{plan_text[:1500]}[/]")
        sys.exit(1)

    if not plan:
        console.print("[red]Empty plan received[/]")
        sys.exit(1)

    # Validate plan
    validation_errors = executor.validate_plan(plan, changed_files)
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
            plan, base, head, config,
            start_from=start_from,
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

