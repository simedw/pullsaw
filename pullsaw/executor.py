"""Step execution loop and plan validation."""

import subprocess
import time
from dataclasses import dataclass

from rich.console import Console

from . import claude_code, git_ops
from .config import Config
from .pathspec import matches_any_pattern, validate_patterns

console = Console()


@dataclass
class ValidationError:
    """A plan validation error."""

    message: str
    fatal: bool = True


def validate_plan(plan: dict, changed_files: dict[str, git_ops.FileStatus]) -> list[ValidationError]:
    """Validate the plan structure and coverage.

    Checks:
    - Every step has required fields
    - Every changed file is covered by at least one pattern
    - Each step covers at least one changed file
    - No overly broad patterns
    """
    errors: list[ValidationError] = []

    if "stack" not in plan:
        errors.append(ValidationError("Plan missing 'stack' field"))
        return errors

    steps = plan["stack"]
    if not steps:
        errors.append(ValidationError("Plan has no steps"))
        return errors

    # Collect all patterns from all steps
    all_patterns: list[str] = []
    for step in steps:
        step_id = step.get("id", "?")

        # Check required fields
        if "allow" not in step:
            errors.append(ValidationError(f"Step {step_id}: missing 'allow' field"))
            continue

        if not step.get("title"):
            errors.append(ValidationError(f"Step {step_id}: missing 'title' field", fatal=False))

        if not step.get("goal"):
            errors.append(ValidationError(f"Step {step_id}: missing 'goal' field", fatal=False))

        step_patterns = step.get("allow", []) + step.get("shared_allow", [])
        all_patterns.extend(step_patterns)

        # Validate individual patterns
        pattern_errors = validate_patterns(step_patterns)
        for err in pattern_errors:
            errors.append(ValidationError(f"Step {step_id}: {err}"))

    # Check every changed file is covered
    uncovered: list[str] = []
    for filepath in changed_files:
        if not matches_any_pattern(filepath, all_patterns):
            uncovered.append(filepath)

    if uncovered:
        errors.append(ValidationError(f"Files not covered by any step: {uncovered}"))

    # Check each step covers at least one file
    for step in steps:
        step_id = step.get("id", "?")
        step_patterns = step.get("allow", []) + step.get("shared_allow", [])

        covers_any = False
        for filepath in changed_files:
            if matches_any_pattern(filepath, step_patterns):
                covers_any = True
                break

        if not covers_any:
            errors.append(
                ValidationError(f"Step {step_id} doesn't cover any changed files", fatal=False)
            )

    return errors


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


def run_command(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run a command with timeout, capturing output."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(
            cmd,
            returncode=124,  # timeout exit code
            stdout=e.stdout or "" if isinstance(e.stdout, str) else "",
            stderr=f"Command timed out after {timeout}s",
        )


def run_command_streaming(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run a command with real-time output streaming."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    stdout_lines: list[str] = []
    try:
        while True:
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
    step: dict,
    prev_branch: str,
    original_head: str,
    config: Config,
    prev_topic: str | None = None,
    is_retry: bool = False,
) -> tuple[str, str, float]:
    """Execute one step. Returns (branch_name, topic, cost_usd)."""
    step_start = time.time()
    total_cost = 0.0

    step_id = step.get("id", "?")
    title = step.get("title", f"Step {step_id}")
    allowlist = step.get("allow", []) + step.get("shared_allow", [])

    branch_name = f"{original_head}-step-{step_id}"
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
        result = claude_code.implement_step(
            step=step,
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
                console.print(f"  [yellow]Claude Code reported error but files were changed: {result.error}[/]")
                console.print(f"  [dim]Continuing with {len(changed)} changed files...[/]")
            else:
                console.print(f"  [red]Claude Code failed: {result.error}[/]")
                raise RuntimeError(f"Step {step_id} implementation failed: {result.error}")

    # Fix loop
    for attempt in range(config.max_fix_attempts):
        # Check allowlist (advisory - warn but don't roll back)
        outside_allowlist = check_allowlist(allowlist)
        if outside_allowlist:
            console.print(f"  [yellow]Files edited outside allowlist: {outside_allowlist}[/]")

        # Run format (if configured)
        if config.format_cmd:
            console.print(f"  [dim]$ {' '.join(config.format_cmd)}[/]")
            format_result = run_command_streaming(config.format_cmd)
            if format_result.returncode == 0:
                console.print("  Format: [green]OK[/]")
            else:
                console.print("  Format: [yellow]Warning[/]")

        # Run check (if configured) - compile, lint, etc.
        check_failed = False
        check_output = ""
        if config.check_cmd:
            console.print(f"  [dim]$ {' '.join(config.check_cmd)}[/]")
            check_result = run_command_streaming(config.check_cmd)
            if check_result.returncode == 0:
                console.print("  Check: [green]OK[/]")
            else:
                console.print(f"  Check: [red]FAIL[/] (attempt {attempt + 1}/{config.max_fix_attempts})")
                check_failed = True
                check_output = check_result.stdout + check_result.stderr

        # Run tests (only if check passed)
        test_failed = False
        test_output = ""
        if not check_failed:
            console.print(f"  [dim]$ {' '.join(config.test_cmd)}[/]")
            test_result = run_command_streaming(config.test_cmd)

            if test_result.returncode == 0:
                console.print("  Tests: [green]PASS[/]")
                break
            else:
                console.print(f"  Tests: [red]FAIL[/] (attempt {attempt + 1}/{config.max_fix_attempts})")
                test_failed = True
                test_output = test_result.stdout + test_result.stderr

        # If both passed, we're done (already broke above)
        # If either failed, ask Claude to fix
        if not check_failed and not test_failed:
            break

        if attempt + 1 >= config.max_fix_attempts:
            console.print("[red]Max fix attempts reached[/]")
            raise RuntimeError(
                f"Step {step_id} failed after {config.max_fix_attempts} attempts"
            )

        # Ask Claude to fix (combine check and test output)
        console.print("  Claude Code: fixing...")
        failure_output = ""
        if check_failed:
            failure_output += f"=== CHECK FAILED ===\n{check_output}\n"
        if test_failed:
            failure_output += f"=== TESTS FAILED ===\n{test_output}\n"

        fix_result = claude_code.fix_failures(
            session_id=session_id or "",
            step=step,
            test_output=failure_output,
            outside_allowlist=outside_allowlist if outside_allowlist else None,
            config=config,
        )

        if fix_result.raw:
            total_cost += fix_result.raw.get("total_cost_usd", 0)

        if not fix_result.success:
            console.print(f"  [red]Fix attempt failed: {fix_result.error}[/]")

    # Commit with Topic/Relative for revup compatibility
    topic = step.get("topic", f"{original_head}-step-{step_id}")
    commit_msg = f"step({step_id}): {title}\n\nTopic: {topic}"
    if prev_topic:
        commit_msg += f"\nRelative: {prev_topic}"
    git_ops.commit(commit_msg)

    # Step summary
    step_duration = time.time() - step_start
    console.print(f"  Committed: [green]step({step_id}): {title}[/]")
    console.print(f"  [dim]Topic: {topic}[/]")
    if prev_topic:
        console.print(f"  [dim]Relative: {prev_topic}[/]")
    console.print(f"  [dim]Duration: {step_duration:.1f}s, Cost: ${total_cost:.4f}[/]")

    return actual_branch, topic, total_cost


def execute(
    plan: dict,
    base: str,
    head: str,
    config: Config,
    start_from: int = 1,
    skip_current: bool = False,
) -> list[str]:
    """Execute all steps in the plan.

    Args:
        plan: The validated plan dict
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
    steps = plan["stack"]
    prev = base
    prev_topic: str | None = None
    branches: list[str] = []

    for i, step in enumerate(steps):
        step_id = step.get("id", i + 1)
        title = step.get("title", f"Step {step_id}")

        # Handle --continue: skip steps before start_from
        if step_id < start_from:
            # These steps are already done, just track their branches/topics
            branch_name = f"{head}-step-{step_id}"
            prev = branch_name
            prev_topic = step.get("topic", f"{head}-step-{step_id}")
            branches.append(branch_name)
            console.print(f"\n[dim][{step_id}/{len(steps)}] {title} (already done)[/]")
            continue

        # Handle --skip: skip the current failed step
        if step_id == start_from and skip_current:
            branch_name = f"{head}-step-{step_id}"
            prev = branch_name
            prev_topic = step.get("topic", f"{head}-step-{step_id}")
            branches.append(branch_name)
            console.print(f"\n[yellow][{step_id}/{len(steps)}] {title} (skipped)[/]")
            continue

        console.print(f"\n[bold][{step_id}/{len(steps)}] {title}[/]")

        # When continuing, the first step we execute is a retry (branch exists, has changes)
        is_retry = (step_id == start_from and start_from > 1)
        prev, prev_topic, step_cost = execute_step(step, prev, head, config, prev_topic, is_retry)
        branches.append(prev)
        total_cost += step_cost

    # Drift detection
    final_branch = prev
    drift = git_ops.diff_name_status(head, final_branch)

    total_duration = time.time() - total_start
    console.print()
    console.print(f"[bold]Total time: {total_duration:.1f}s ({total_duration/60:.1f} min), Cost: ${total_cost:.4f}[/]")

    if drift:
        console.print("[yellow]Drift detected vs original branch:[/]")
        for filepath, status in drift.items():
            console.print(f"  {status} {filepath}")

        if config.strict:
            raise RuntimeError("Drift detected in strict mode")
    else:
        console.print("[green]No drift - stack matches original![/]")

    return branches

