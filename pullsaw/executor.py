"""Step execution loop."""

import subprocess
import time

from rich.console import Console

from . import claude_code, git_ops
from .config import Config
from .models import Plan, Step
from .pathspec import matches_any_pattern

console = Console()


def check_allowlist(patterns: list[str]) -> list[str]:
    """Check for files edited outside the allowlist (advisory only).

    Returns list of files outside the allowlist but does NOT roll them back.
    """
    changed = git_ops.changed_files_working()
    outside: list[str] = []

    # Files we always allow (pullsaw manages these)
    always_allow = {".gitignore", ".pullsaw"}

    for filepath in changed:
        if filepath in always_allow or filepath.startswith(".pullsaw/"):
            continue
        if not matches_any_pattern(filepath, patterns):
            outside.append(filepath)

    return outside


def run_command(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a command with timeout, capturing output."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        stdout = ""
        if e.stdout:
            stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else e.stdout
        return subprocess.CompletedProcess(
            cmd,
            returncode=124,  # timeout exit code
            stdout=stdout,
            stderr=f"Command timed out after {timeout}s",
        )


def run_command_streaming(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a command with real-time output streaming."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    stdout_lines: list[str] = []
    start_time = time.time()

    try:
        while True:
            # Check timeout
            if time.time() - start_time > timeout:
                process.kill()
                return subprocess.CompletedProcess(
                    cmd,
                    returncode=124,
                    stdout="".join(stdout_lines),
                    stderr=f"Command timed out after {timeout}s",
                )

            # Check if process is done
            retcode = process.poll()

            # Read available output
            if process.stdout:
                line = process.stdout.readline()
                if line:
                    # Print with indentation and dim style
                    console.print(f"    [dim]{line.rstrip()}[/]")
                    stdout_lines.append(line)
                elif retcode is not None:
                    break
            elif retcode is not None:
                break

    except Exception:
        process.kill()
        raise

    return subprocess.CompletedProcess(
        cmd,
        returncode=process.returncode or 0,
        stdout="".join(stdout_lines),
        stderr="",
    )


def execute_step(
    step: Step,
    prev_branch: str,
    original_head: str,
    config: Config,
    prev_topic: str | None = None,
    is_retry: bool = False,
) -> tuple[str, str, float]:
    """Execute one step. Returns (branch_name, topic, cost_usd)."""
    step_start = time.time()
    total_cost = 0.0

    allowlist = step.all_patterns
    branch_name = f"{original_head}-step-{step.id}"
    session_id: str | None = None

    if is_retry:
        # Retrying a failed step - branch exists, skip implementation
        console.print(f"  Retrying on branch: [cyan]{branch_name}[/]")
        actual_branch = branch_name
    else:
        # Create branch
        actual_branch = git_ops.create_branch(branch_name, prev_branch)
        console.print(f"  Created branch: [cyan]{actual_branch}[/]")

        # Initial implementation
        console.print("  Claude Code: implementing...")

        # Convert Step to dict for claude_code (maintains compatibility)
        step_dict = step.to_dict()
        result = claude_code.implement_step(
            step=step_dict,
            current_branch=actual_branch,
            prev_branch=prev_branch,
            original_head=original_head,
            config=config,
        )

        session_id = result.session_id
        if result.raw:
            total_cost += result.raw.get("total_cost_usd", 0)

        if not result.success:
            # Check if files were changed despite the error (e.g., timeout after work was done)
            changed = git_ops.changed_files_working()
            if changed:
                console.print(
                    f"  [yellow]Claude Code reported error but files were changed: {result.error}[/]"
                )
                console.print(f"  [dim]Continuing with {len(changed)} changed files...[/]")
            else:
                console.print(f"  [red]Claude Code failed: {result.error}[/]")
                raise RuntimeError(f"Step {step.id} implementation failed: {result.error}")

    # Fix loop
    for attempt in range(config.max_fix_attempts):
        # Check allowlist (advisory - warn but don't roll back)
        outside_allowlist = check_allowlist(allowlist)
        if outside_allowlist:
            console.print(f"  [yellow]Files edited outside allowlist: {outside_allowlist}[/]")

        # Run format (if configured)
        if config.format_cmd:
            console.print(f"  [dim]$ {' '.join(config.format_cmd)}[/]")
            format_result = run_command_streaming(config.format_cmd, config.command_timeout)
            if format_result.returncode == 0:
                console.print("  Format: [green]OK[/]")
            else:
                console.print("  Format: [yellow]Warning[/]")

        # Run check (if configured) - compile, lint, etc.
        check_failed = False
        check_output = ""
        if config.check_cmd:
            console.print(f"  [dim]$ {' '.join(config.check_cmd)}[/]")
            check_result = run_command_streaming(config.check_cmd, config.command_timeout)
            if check_result.returncode == 0:
                console.print("  Check: [green]OK[/]")
            else:
                console.print(
                    f"  Check: [red]FAIL[/] (attempt {attempt + 1}/{config.max_fix_attempts})"
                )
                check_failed = True
                check_output = check_result.stdout + check_result.stderr

        # Run tests (only if check passed)
        test_failed = False
        test_output = ""
        if not check_failed:
            console.print(f"  [dim]$ {' '.join(config.test_cmd)}[/]")
            test_result = run_command_streaming(config.test_cmd, config.test_timeout)

            if test_result.returncode == 0:
                console.print("  Tests: [green]PASS[/]")
                break
            else:
                console.print(
                    f"  Tests: [red]FAIL[/] (attempt {attempt + 1}/{config.max_fix_attempts})"
                )
                test_failed = True
                test_output = test_result.stdout + test_result.stderr

        # If both passed, we're done (already broke above)
        # If either failed, ask Claude to fix
        if not check_failed and not test_failed:
            break

        if attempt + 1 >= config.max_fix_attempts:
            console.print("[red]Max fix attempts reached[/]")
            raise RuntimeError(f"Step {step.id} failed after {config.max_fix_attempts} attempts")

        # Ask Claude to fix (combine check and test output)
        console.print("  Claude Code: fixing...")
        failure_output = ""
        if check_failed:
            failure_output += f"=== CHECK FAILED ===\n{check_output}\n"
        if test_failed:
            failure_output += f"=== TESTS FAILED ===\n{test_output}\n"

        # Convert Step to dict for claude_code
        step_dict = step.to_dict()
        fix_result = claude_code.fix_failures(
            session_id=session_id or "",
            step=step_dict,
            test_output=failure_output,
            outside_allowlist=outside_allowlist if outside_allowlist else None,
            config=config,
        )

        if fix_result.raw:
            total_cost += fix_result.raw.get("total_cost_usd", 0)

        if not fix_result.success:
            console.print(f"  [red]Fix attempt failed: {fix_result.error}[/]")

    # Commit with Topic/Relative for revup compatibility
    topic = step.topic or f"{original_head}-step-{step.id}"
    commit_msg = f"step({step.id}): {step.title}\n\nTopic: {topic}"
    if prev_topic:
        commit_msg += f"\nRelative: {prev_topic}"
    git_ops.commit(commit_msg)

    # Step summary
    step_duration = time.time() - step_start
    console.print(f"  Committed: [green]step({step.id}): {step.title}[/]")
    console.print(f"  [dim]Topic: {topic}[/]")
    if prev_topic:
        console.print(f"  [dim]Relative: {prev_topic}[/]")
    console.print(f"  [dim]Duration: {step_duration:.1f}s, Cost: ${total_cost:.4f}[/]")

    return actual_branch, topic, total_cost


def execute(
    plan: Plan,
    base: str,
    head: str,
    config: Config,
    start_from: int = 1,
    skip_current: bool = False,
) -> list[str]:
    """Execute all steps in the plan.

    Args:
        plan: The validated Plan object
        base: Base branch name
        head: Original head branch name
        config: PullSaw configuration
        start_from: Step ID to start from (for --continue)
        skip_current: If True, skip the start_from step and go to next

    Returns:
        List of created branch names
    """
    total_start = time.time()
    total_cost = 0.0
    prev = base
    prev_topic: str | None = None
    branches: list[str] = []

    for step in plan.steps:
        # Handle --continue: skip steps before start_from
        if step.id < start_from:
            # These steps are already done, just track their branches/topics
            branch_name = f"{head}-step-{step.id}"
            prev = branch_name
            prev_topic = step.topic or f"{head}-step-{step.id}"
            branches.append(branch_name)
            console.print(f"\n[dim][{step.id}/{len(plan)}] {step.title} (already done)[/]")
            continue

        # Handle --skip: skip the current failed step
        if step.id == start_from and skip_current:
            branch_name = f"{head}-step-{step.id}"
            prev = branch_name
            prev_topic = step.topic or f"{head}-step-{step.id}"
            branches.append(branch_name)
            console.print(f"\n[yellow][{step.id}/{len(plan)}] {step.title} (skipped)[/]")
            continue

        console.print(f"\n[bold][{step.id}/{len(plan)}] {step.title}[/]")

        # When continuing, the first step we execute is a retry (branch exists, has changes)
        is_retry = step.id == start_from and start_from > 1
        prev, prev_topic, step_cost = execute_step(step, prev, head, config, prev_topic, is_retry)
        branches.append(prev)
        total_cost += step_cost

    # Drift detection
    final_branch = prev
    drift = git_ops.diff_name_status(head, final_branch)

    total_duration = time.time() - total_start
    console.print()
    console.print(
        f"[bold]Total time: {total_duration:.1f}s ({total_duration / 60:.1f} min), Cost: ${total_cost:.4f}[/]"
    )

    if drift:
        console.print("[yellow]Drift detected vs original branch:[/]")
        for filepath, status in drift.items():
            console.print(f"  {status} {filepath}")

        if config.strict:
            raise RuntimeError("Drift detected in strict mode")
    else:
        console.print("[green]No drift - stack matches original![/]")

    return branches
