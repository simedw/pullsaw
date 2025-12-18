"""Headless Claude Code wrapper using claude -p."""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .constants import PULLSAW_DIR


@dataclass
class ClaudeResult:
    """Result from a Claude Code invocation."""

    success: bool
    session_id: str | None
    output: str
    error: str | None
    raw: dict


def invoke(
    prompt: str,
    allowed_tools: list[str] | None = None,
    system_prompt: str | None = None,
    resume_session: str | None = None,
    timeout: int = 300,  # 5 min default
    output_format: str = "json",  # "json" or "text"
    max_turns: int | None = None,
) -> ClaudeResult:
    """Run claude -p with proper error handling.

    Args:
        prompt: The prompt to send
        allowed_tools: List of tools to allow
        system_prompt: Additional system prompt
        resume_session: Session ID to resume
        timeout: Timeout in seconds
        output_format: "json" for structured output, "text" for raw output
        max_turns: Limit number of conversation turns
    """
    cmd = ["claude", "-p", "--output-format", output_format]

    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    if resume_session:
        cmd.extend(["--resume", resume_session])

    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])

    # Pass prompt via stdin to handle long prompts and special characters
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ClaudeResult(
            success=False,
            session_id=None,
            output="",
            error="Command timed out",
            raw={},
        )

    # Handle text format (simpler, no JSON parsing)
    if output_format == "text":
        return ClaudeResult(
            success=result.returncode == 0,
            session_id=None,  # Not available in text format
            output=result.stdout,
            error=result.stderr if result.returncode != 0 else None,
            raw={},
        )

    # Handle JSON format
    try:
        data = json.loads(result.stdout)

        # Check all success indicators
        is_error = data.get("is_error", False)
        subtype = data.get("subtype", "")

        success = result.returncode == 0 and not is_error and subtype in ("success", "")

        return ClaudeResult(
            success=success,
            session_id=data.get("session_id"),
            output=data.get("result", ""),
            error=data.get("error") if is_error else None,
            raw=data,
        )
    except json.JSONDecodeError:
        return ClaudeResult(
            success=False,
            session_id=None,
            output=result.stdout,
            error=f"JSON parse failed. stderr: {result.stderr}",
            raw={},
        )


def invoke_streaming(
    prompt: str,
    allowed_tools: list[str] | None = None,
    system_prompt: str | None = None,
    resume_session: str | None = None,
    timeout: int = 1800,  # 30 min default for streaming (implementation can take a while)
    on_message: Callable | None = None,
) -> ClaudeResult:
    """Run claude -p with streaming output.

    Uses stream-json format to display progress in real-time.

    Args:
        prompt: The prompt to send
        allowed_tools: List of tools to allow
        system_prompt: Additional system prompt
        resume_session: Session ID to resume
        timeout: Timeout in seconds
        on_message: Callback for each message (receives parsed JSON)
    """
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]

    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])

    if resume_session:
        cmd.extend(["--resume", resume_session])

    from rich.console import Console

    console = Console()

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Send prompt via stdin (these are guaranteed non-None when PIPE is used)
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    process.stdin.write(prompt)
    process.stdin.close()

    result_data: dict = {}
    session_id: str | None = None
    output_text = ""

    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            # Handle different message types
            if msg_type == "assistant":
                # Assistant message - show content
                content = msg.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            console.print(f"[dim]{text[:200]}{'...' if len(text) > 200 else ''}[/]")
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown")
                        console.print(f"[cyan]→ {tool_name}[/]")

            elif msg_type == "result":
                # Final result with stats
                result_data = msg
                session_id = msg.get("session_id")
                output_text = msg.get("result", "")

            # Call custom handler if provided
            if on_message:
                on_message(msg)

        process.wait(timeout=timeout)

    except subprocess.TimeoutExpired:
        process.kill()
        return ClaudeResult(
            success=False,
            session_id=None,
            output="",
            error="Command timed out",
            raw={},
        )

    stderr = process.stderr.read()

    is_error = result_data.get("is_error", False)
    subtype = result_data.get("subtype", "")
    success = process.returncode == 0 and not is_error and subtype in ("success", "")

    return ClaudeResult(
        success=success,
        session_id=session_id,
        output=output_text,
        error=result_data.get("error") if is_error else (stderr if stderr else None),
        raw=result_data,
    )


def _build_plan_tools() -> list[str]:
    """Build the tool list for planning phase."""
    return [
        "Read",
        "Grep",
        "Bash(git diff)",
        "Bash(git status)",
        "Bash(git log)",
    ]


def _build_impl_tools(config: Config) -> list[str]:
    """Build the tool list for implementation phase."""
    tools = [
        "Read",
        "Grep",
        "Write",
        "Edit",
        "Bash(git diff)",
        "Bash(git status)",
        "Bash(git show)",
        "Bash(git checkout)",  # For copying files from target branch
    ]

    # Only add format command - tests are run by the executor
    # This prevents Claude from running slow tests redundantly
    if config.format_cmd:
        format_cmd_str = " ".join(config.format_cmd)
        tools.append(f"Bash({format_cmd_str})")

    return tools


def _build_fix_tools(config: Config) -> list[str]:
    """Build the tool list for fix phase - includes test/check commands."""
    tools = _build_impl_tools(config)

    # During fix phase, Claude can run tests/checks to verify fixes
    if config.check_cmd:
        check_cmd_str = " ".join(config.check_cmd)
        tools.append(f"Bash({check_cmd_str})")

    if config.test_cmd:
        test_cmd_str = " ".join(config.test_cmd)
        tools.append(f"Bash({test_cmd_str})")

    return tools


def get_plan_file(repo_root: str, branch: str) -> str:
    """Get the plan file path for a branch."""
    # Sanitize branch name for filename
    safe_branch = branch.replace("/", "-").replace(" ", "-")
    return f"{repo_root}/{PULLSAW_DIR}/{safe_branch}-plan.yaml"


def generate_plan(
    base: str,
    head: str,
    name_status: str,
    stat: str,
    plan_file: str,
) -> ClaudeResult:
    """Generate a stacked PR plan using Claude Code.

    Claude writes the plan to a file to avoid stdout truncation.

    Args:
        base: Base branch name
        head: Head branch name
        name_status: Output of git diff --name-status
        stat: Output of git diff --stat
        plan_file: Path to write the plan YAML
    """
    prompt = f"""Analyze this repository and propose a stacked PR plan.

BASE: {base}
HEAD: {head}

CHANGE SUMMARY:
{stat}

FILE CHANGES (status + path):
{name_status}

You can use Read/Grep to inspect files if needed.

IMPORTANT: Write the plan YAML to {plan_file}
Do not output the YAML to stdout - write it to the file to avoid truncation.

The file should contain valid YAML like:

stack:
  - id: 1
    title: "Short title"
    goal: "Brief goal"
    topic: "branch-name-feature-aspect"
    allow:
      - "lib/feature/**"
  - id: 2
    title: "Next step"
    goal: "Brief goal"
    topic: "branch-name-next-aspect"
    allow:
      - "lib/other/**"

RULES:
- 4-5 steps MAXIMUM
- Use BROAD globs: "lib/foo/**" NOT individual files
- Skip invariants field (assume "Tests pass")
- Skip shared_allow unless essential
- Every changed file must match at least one allow pattern

CO-LOCATE TESTS WITH CODE (CRITICAL):
- Each step MUST include the tests for the code in that step
- NEVER put all tests in the final step
- If step 1 adds "lib/myapp/foo.ex", it must also include "test/myapp/foo_test.exs"
- Pattern: "lib/myapp/feature/**" should be paired with "test/myapp/feature/**"
- Each PR should be independently testable - reviewers need tests with the code
- BAD: Step 5 with "test/**" catching all tests
- GOOD: Each step has its own test patterns alongside implementation patterns

TOPIC NAMING:
- Each step needs a unique "topic" field for revup compatibility
- Combine the branch name with the step's purpose
- Example for branch "user-auth": "user-auth-models", "user-auth-routes", "user-auth-tests"
- Keep topics short, lowercase, hyphenated
- Topics are used for stacked PR dependencies

Write the complete plan to {plan_file} now.
"""
    return invoke(
        prompt,
        allowed_tools=_build_plan_tools() + ["Write"],
        timeout=600,  # 10 min for large PRs
        output_format="json",
        max_turns=20,  # Allow enough turns for large PRs
    )


def implement_step(
    step: dict,
    current_branch: str,
    prev_branch: str,
    original_head: str,
    config: Config,
    streaming: bool = True,
) -> ClaudeResult:
    """Implement a single step of the stacked PR.

    Args:
        step: Step dict with id, title, goal, allow, shared_allow, invariants
        current_branch: The branch we just created for this step
        prev_branch: The branch we branched from
        original_head: The original feature branch to reference
        config: PullSaw configuration
        streaming: Whether to stream output in real-time
    """
    allowlist = step.get("allow", []) + step.get("shared_allow", [])
    format_cmd_str = " ".join(config.format_cmd) if config.format_cmd else "mix format"

    system = f"""FILE EDITING GUIDELINES:
- PRIMARY FILES (focus here): {allowlist}
- Changes must move files TOWARD the final state at {original_head}
- If adding a shim not in final branch, mark with # TODO(pullsaw): remove
- Make MINIMAL changes to satisfy the goal

OUTSIDE THE PRIMARY LIST:
- You MAY edit files outside the list IF compilation/tests require it (e.g., boundary configs, shared types)
- When you do, explain WHY it was necessary
- Keep such edits MINIMAL - only what's needed to unblock the primary work
- Prefer shims/stubs in allowed files over editing external files when possible

APPROACH:
- Use `git show {original_head}:<filepath>` to see exact file contents from target branch
- Use `git checkout {original_head} -- <filepath>` to copy files directly when appropriate
- For migration files: use EXACT filenames from target branch, don't generate new timestamps
- For partial changes: use git show to see the target, then edit to match"""

    prompt = f"""Implement step {step["id"]} of a stacked PR split.

BRANCHES:
- Current branch: {current_branch}
- Based on: {prev_branch}
- Target reference: {original_head}

GOAL: {step["goal"]}
TITLE: {step["title"]}

STRATEGY:
1. First, check what files changed: `git diff --name-only {prev_branch}..{original_head} -- <allowed_patterns>`
2. For each file in the allowed list:
   - Use `git show {original_head}:<filepath>` to see the target state
   - Either `git checkout {original_head} -- <filepath>` to copy directly
   - Or edit incrementally if only partial changes needed for this step
3. Keep exact filenames from target (especially for migrations)

ALLOWED FILES (only edit these):
{chr(10).join(allowlist)}

Implement the changes now. Use `{format_cmd_str}` to verify code compiles.
DO NOT run tests - the executor will run them after you finish.
"""
    if streaming:
        return invoke_streaming(
            prompt,
            allowed_tools=_build_impl_tools(config),
            system_prompt=system,
        )
    return invoke(
        prompt,
        allowed_tools=_build_impl_tools(config),
        system_prompt=system,
    )


def fix_failures(
    session_id: str,
    step: dict,
    test_output: str,
    outside_allowlist: list[str] | None = None,
    config: Config | None = None,
    streaming: bool = True,
) -> ClaudeResult:
    """Fix test failures by resuming the Claude session.

    Args:
        session_id: The session ID to resume
        step: Step dict with allow/shared_allow patterns
        test_output: The test failure output
        outside_allowlist: List of files edited outside the allowlist (advisory)
        config: PullSaw configuration (for tool permissions)
        streaming: Whether to stream output in real-time
    """
    allowlist = step.get("allow", []) + step.get("shared_allow", [])

    context = ""
    if outside_allowlist:
        context = f"""
NOTE: You edited files outside the primary allowlist:
{chr(10).join(outside_allowlist)}
This is allowed when necessary, but keep external edits minimal.
"""

    prompt = f"""Tests/checks failed. Fix with minimal changes.
{context}
PRIMARY FILES: {chr(10).join(allowlist)}

If the fix requires editing files outside this list (e.g., boundary configs,
shared modules), you may do so - but explain why and keep it minimal.

FAILURE OUTPUT (last 3000 chars):
{test_output[-3000:]}

TIP: You can run tests with --failed flag to rerun only previously failed tests (faster iteration).
"""

    # Use fix tools (includes test/check commands so Claude can verify fixes)
    tools = _build_fix_tools(config) if config else None

    if streaming:
        return invoke_streaming(
            prompt,
            allowed_tools=tools,
            resume_session=session_id,
        )
    return invoke(
        prompt,
        allowed_tools=tools,
        resume_session=session_id,
    )
