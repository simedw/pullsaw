"""Session management for PullSaw runs."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from . import claude_code, git_ops
from .config import Config
from .constants import PULLSAW_DIR
from .git_ops import FileStatus
from .models import Plan

console = Console()


@dataclass
class ContinueInfo:
    """Information extracted from --continue mode."""

    derived_head: str
    start_from: int
    plan_path: str


@dataclass
class Session:
    """Represents a single PullSaw run with all necessary state."""

    repo_root: Path
    base: str
    head: str
    config: Config
    plan: Plan | None = None
    changed_files: dict[str, FileStatus] = field(default_factory=dict)
    verbose: bool = False

    # Internal state
    _merge_base: str | None = None
    _continue_info: ContinueInfo | None = None

    @classmethod
    def from_args(
        cls,
        base: str | None = None,
        head: str | None = None,
        strict: bool = False,
        test_cmd: str | None = None,
        check_cmd: str | None = None,
        continue_run: bool = False,
        skip: bool = False,
        verbose: bool = False,
    ) -> "Session":
        """Create a Session from CLI arguments.

        Handles:
        - --continue mode detection from current branch
        - Branch inference when base/head not specified
        - Config loading and command overrides

        Args:
            base: Base branch name (auto-detect if None)
            head: Head branch name (auto-detect if None)
            strict: Enable strict drift checking
            test_cmd: Override test command
            check_cmd: Override check command
            continue_run: Whether we're continuing from a step branch
            skip: Whether to skip the current step (with --continue)
            verbose: Enable verbose output

        Returns:
            Configured Session instance

        Raises:
            ValueError: If branch inference fails or --continue is invalid
        """
        repo_root = git_ops.get_repo_root()
        continue_info: ContinueInfo | None = None

        # Handle --continue: detect step from current branch
        if continue_run:
            current = git_ops.current_branch()
            match = re.match(r"^(.+)-step-(\d+)$", current)
            if not match:
                raise ValueError(
                    f"Current branch '{current}' doesn't match pattern '{{head}}-step-N'. "
                    "Run --continue from a step branch (e.g., my-feature-step-3)"
                )

            derived_head = match.group(1)
            start_from = int(match.group(2))
            plan_file_path = claude_code.get_plan_file(str(repo_root), derived_head)

            continue_info = ContinueInfo(
                derived_head=derived_head,
                start_from=start_from,
                plan_path=plan_file_path,
            )

            # Auto-set head from branch name
            if head is None:
                head = derived_head

            action = "Skipping" if skip else "Retrying"
            console.print(f"[cyan]Continuing from step {start_from}[/] ({action})")
            console.print(f"[dim]Original head: {derived_head}[/]")

        # Infer branches if not provided
        if base is None or head is None:
            inferred_base, inferred_head = git_ops.infer_branches()
            base = base or inferred_base
            head = head or inferred_head

        # Load config
        config = Config.load(repo_root)
        config.strict = strict or config.strict

        # Override commands if provided
        if test_cmd:
            config.test_cmd = test_cmd.split()
        if check_cmd:
            config.check_cmd = check_cmd.split()

        session = cls(
            repo_root=repo_root,
            base=base,
            head=head,
            config=config,
            verbose=verbose,
        )
        session._continue_info = continue_info

        return session

    @property
    def merge_base(self) -> str:
        """Get the merge-base commit (cached)."""
        if self._merge_base is None:
            self._merge_base = git_ops.merge_base(self.base, self.head)
        return self._merge_base

    @property
    def start_from(self) -> int:
        """Get the step to start from (1 unless --continue)."""
        if self._continue_info:
            return self._continue_info.start_from
        return 1

    @property
    def is_continuing(self) -> bool:
        """Whether this is a --continue run."""
        return self._continue_info is not None

    def setup_pullsaw_dir(self) -> bool:
        """Create .pullsaw/ directory, template config, and update .gitignore.

        Returns:
            True if .gitignore was modified, False otherwise
        """
        pullsaw_dir = self.repo_root / PULLSAW_DIR
        is_new_dir = not pullsaw_dir.exists()
        pullsaw_dir.mkdir(exist_ok=True)

        # Create template config.yml if directory is new
        config_path = pullsaw_dir / "config.yml"
        if is_new_dir and not config_path.exists():
            template = Config.generate_template(self.repo_root)
            config_path.write_text(template)
            console.print(f"[dim]Created {PULLSAW_DIR}/config.yml with auto-detected settings[/]")

        gitignore = self.repo_root / ".gitignore"
        gitignore_entry = f"/{PULLSAW_DIR}/"
        gitignore_modified = False

        if gitignore.exists():
            content = gitignore.read_text()
            # Check line-by-line to avoid partial matches (e.g., ".pullsaw2")
            lines = [line.strip() for line in content.splitlines()]
            valid_entries = (gitignore_entry, PULLSAW_DIR, f"{PULLSAW_DIR}/")
            has_entry = any(line in valid_entries for line in lines)
            if not has_entry:
                with open(gitignore, "a") as f:
                    f.write(f"\n# PullSaw working directory\n{gitignore_entry}\n")
                console.print(f"[dim]Added {gitignore_entry} to .gitignore[/]")
                gitignore_modified = True
        else:
            gitignore.write_text(f"# PullSaw working directory\n{gitignore_entry}\n")
            console.print(f"[dim]Created .gitignore with {gitignore_entry}[/]")
            gitignore_modified = True

        return gitignore_modified

    def ensure_clean_working_tree(self, allow_gitignore: bool = False) -> None:
        """Ensure the working tree is clean.

        Args:
            allow_gitignore: If True, allow .gitignore to be modified

        Raises:
            RuntimeError: If working tree has uncommitted changes
        """
        if self.is_continuing:
            # When continuing, we expect uncommitted changes
            changed = git_ops.changed_files_working()
            console.print(f"Working tree: [yellow]{len(changed)} uncommitted files[/] (continuing)")
            return

        console.print("Checking working tree...", end=" ")
        if not git_ops.is_clean():
            changed = git_ops.changed_files_working()
            only_gitignore = allow_gitignore and set(changed.keys()) == {".gitignore"}
            if not only_gitignore:
                console.print("[red]dirty[/]")
                raise RuntimeError("Working tree has uncommitted changes. Commit or stash first.")
        console.print("[green]clean[/]")

    def load_changed_files(self) -> dict[str, FileStatus]:
        """Load the changed files between merge-base and head.

        Returns:
            Dict mapping filepath to FileStatus
        """
        self.changed_files = git_ops.changed_files(self.merge_base, self.head)
        return self.changed_files

    def get_diff_info(self) -> tuple[str, str]:
        """Get diff info for plan generation.

        Returns:
            Tuple of (name_status, stat) strings
        """
        name_status = git_ops.run_git(
            "diff", "--name-status", f"{self.merge_base}..{self.head}"
        ).stdout
        stat = git_ops.diff_stat(self.merge_base, self.head)
        return name_status, stat

    def load_or_generate_plan(self, plan_path: str | None = None) -> Plan:
        """Load an existing plan or generate a new one via Claude Code.

        Args:
            plan_path: Path to existing plan file, or None to generate

        Returns:
            Loaded/generated Plan

        Raises:
            RuntimeError: If plan generation or loading fails
        """
        # Use continue plan path if available and no explicit path
        if plan_path is None and self._continue_info:
            plan_path = self._continue_info.plan_path

        if plan_path:
            plan_file = Path(plan_path)
            if not plan_file.exists():
                raise RuntimeError(f"Plan file not found: {plan_file}")
            console.print(f"\n[bold]Using existing plan:[/] {plan_file}")
        else:
            # Generate plan via Claude Code
            console.print("\n[bold]Generating plan via Claude Code...[/]")

            plan_file_path = claude_code.get_plan_file(str(self.repo_root), self.head)
            plan_file = Path(plan_file_path)

            # Clean up old plan file if exists
            if plan_file.exists():
                plan_file.unlink()

            # Get diff info for planning
            name_status, stat = self.get_diff_info()

            plan_result = claude_code.generate_plan(
                self.base, self.head, name_status, stat, plan_file_path
            )

            if not plan_result.success:
                error_msg = f"Failed to generate plan: {plan_result.error}"
                if plan_result.output:
                    error_msg += f"\nOutput: {plan_result.output[:500]}"
                raise RuntimeError(error_msg)

            # Show result info
            if self.verbose:
                console.print(f"\n[dim]Session: {plan_result.session_id}[/]")
                if plan_result.raw:
                    console.print(
                        f"[dim]Turns: {plan_result.raw.get('num_turns')}, "
                        f"Cost: ${plan_result.raw.get('total_cost_usd', 0):.4f}[/]"
                    )
            if plan_result.error:
                console.print(f"[yellow]Warning: {plan_result.error}[/]")

        # Verify plan file exists
        if not plan_file.exists():
            raise RuntimeError(f"Plan file not found after generation: {plan_file}")

        # Load and parse plan
        try:
            self.plan = Plan.from_yaml(plan_file)
        except Exception as e:
            raise RuntimeError(f"Failed to parse plan: {e}") from e

        if self.verbose:
            console.print(f"[dim]Plan file: {plan_file}[/]")

        return self.plan

    def print_info(self) -> None:
        """Print session info (branches, config)."""
        console.print(f"Base: [cyan]{self.base}[/]")
        console.print(f"Head: [cyan]{self.head}[/]")

        if self.verbose:
            console.print(f"Merge base: [dim]{self.merge_base[:12]}[/]")

        if self.changed_files:
            console.print(f"Changed files: [green]{len(self.changed_files)}[/]")

            if self.verbose:
                for filepath, status in sorted(self.changed_files.items()):
                    console.print(f"  [dim]{status.value} {filepath}[/]")

        console.print(f"Test command: [dim]{' '.join(self.config.test_cmd)}[/]")
        if self.config.check_cmd:
            console.print(f"Check command: [dim]{' '.join(self.config.check_cmd)}[/]")
        if self.config.format_cmd:
            console.print(f"Format command: [dim]{' '.join(self.config.format_cmd)}[/]")
