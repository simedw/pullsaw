"""Git operations with robust handling of all file status types."""

import re
import subprocess
from contextlib import suppress
from enum import Enum
from pathlib import Path


class GitError(Exception):
    """Exception raised when a git command fails.

    Attributes:
        command: The git command that failed
        returncode: The exit code
        stderr: The error output from git
    """

    def __init__(self, command: list[str], returncode: int, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        cmd_str = " ".join(command)
        message = f"Git command failed: {cmd_str}\n{stderr.strip()}"
        super().__init__(message)


class FileStatus(Enum):
    """Git file status."""

    MODIFIED = "M"
    ADDED = "A"
    DELETED = "D"
    RENAMED = "R"
    COPIED = "C"
    UNTRACKED = "?"


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result.

    Args:
        *args: Git command arguments
        check: If True, raise GitError on non-zero exit

    Raises:
        GitError: If check=True and the command fails
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,  # We handle checking ourselves
    )

    if check and result.returncode != 0:
        raise GitError(["git", *args], result.returncode, result.stderr)

    return result


def is_clean() -> bool:
    """Check for uncommitted changes."""
    result = run_git("status", "--porcelain", check=False)
    return result.stdout.strip() == ""


def get_repo_root() -> Path:
    """Get the repository root directory."""
    result = run_git("rev-parse", "--show-toplevel")
    return Path(result.stdout.strip())


def current_branch() -> str:
    """Get current branch name."""
    result = run_git("rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


def infer_branches() -> tuple[str, str]:
    """Return (base, head). Fails if on main/master."""
    head = current_branch()

    if head in ("main", "master"):
        raise ValueError("Cannot run on main/master branch")

    # Detect base branch
    for candidate in ("main", "master"):
        result = run_git("rev-parse", "--verify", candidate, check=False)
        if result.returncode == 0:
            return candidate, head

    raise ValueError("Could not find main or master branch")


def changed_files(base: str, head: str) -> dict[str, FileStatus]:
    """Get changed files between refs with their status.

    Uses --name-status for reliable parsing.
    """
    result = run_git("diff", "--name-status", f"{base}..{head}")

    files: dict[str, FileStatus] = {}
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        status_char = parts[0][0]  # First char (R100 -> R)
        filepath = parts[-1]  # Last part (handles renames: old -> new)

        try:
            files[filepath] = FileStatus(status_char)
        except ValueError:
            # Unknown status, treat as modified
            files[filepath] = FileStatus.MODIFIED

    return files


def changed_files_working() -> dict[str, FileStatus]:
    """Get uncommitted changes in working tree.

    Uses porcelain v2 with NUL separator for reliable parsing.
    """
    result = run_git("status", "--porcelain=v2", "-z", check=False)

    files: dict[str, FileStatus] = {}
    if not result.stdout:
        return files

    # Split by NUL and process entries
    entries = result.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        if not entry:
            i += 1
            continue

        # Porcelain v2 format:
        # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
        # 2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path><sep><origPath>
        # ? <path>
        # ! <path>

        if entry.startswith("1 "):
            # Ordinary changed entry
            parts = entry.split(" ", 8)
            if len(parts) >= 9:
                xy = parts[1]
                filepath = parts[8]
                status = _parse_xy_status(xy)
                files[filepath] = status
        elif entry.startswith("2 "):
            # Renamed/copied entry - next entry is the original path
            parts = entry.split(" ", 9)
            if len(parts) >= 10:
                filepath = parts[9]
                files[filepath] = FileStatus.RENAMED
            i += 1  # Skip the original path entry
        elif entry.startswith("? "):
            # Untracked
            filepath = entry[2:]
            files[filepath] = FileStatus.UNTRACKED
        elif entry.startswith("! "):
            # Ignored - skip
            pass

        i += 1

    return files


def _parse_xy_status(xy: str) -> FileStatus:
    """Parse XY status from porcelain v2 format."""
    # X = index status, Y = worktree status
    # We care about either being set
    x, y = xy[0], xy[1]

    if x == "D" or y == "D":
        return FileStatus.DELETED
    if x == "A":
        return FileStatus.ADDED
    if x == "R":
        return FileStatus.RENAMED
    if x == "C":
        return FileStatus.COPIED
    return FileStatus.MODIFIED


def checkout_files(files: list[str]) -> None:
    """Restore files to HEAD state (handles add/modify/delete).

    Uses git restore for tracked files, removes untracked files.
    """
    for filepath in files:
        # Check if file exists in HEAD
        result = run_git("cat-file", "-e", f"HEAD:{filepath}", check=False)

        if result.returncode == 0:
            # File exists in HEAD - restore it
            run_git(
                "restore", "--source=HEAD", "--staged", "--worktree", "--", filepath, check=False
            )
        else:
            # File doesn't exist in HEAD (newly added) - remove it
            run_git("rm", "-f", "--", filepath, check=False)
            # Also remove from working tree if still there (untracked)
            with suppress(FileNotFoundError):
                Path(filepath).unlink()


def sanitize_branch_name(name: str) -> str:
    """Sanitize a string to be a valid git branch name.

    Git branch names cannot contain:
    - Space, ~, ^, :, ?, *, [, \\ (backslash)
    - Consecutive dots (..)
    - Leading or trailing dots or slashes
    - @{ sequence
    - Control characters

    This function replaces problematic characters with hyphens and
    cleans up the result.
    """
    # Replace problematic characters with hyphens
    # Covers: space, ~, ^, :, ?, *, [, ], \, @
    safe_name = re.sub(r"[\s~^:?*\[\]\\@/]+", "-", name)

    # Remove consecutive dots (.. is invalid in git refs)
    safe_name = re.sub(r"\.{2,}", ".", safe_name)

    # Remove leading/trailing dots and hyphens
    safe_name = safe_name.strip(".-")

    # Collapse multiple consecutive hyphens
    safe_name = re.sub(r"-{2,}", "-", safe_name)

    return safe_name


def create_branch(name: str, from_ref: str) -> str:
    """Create and checkout a new branch. Returns the sanitized branch name."""
    safe_name = sanitize_branch_name(name)
    run_git("checkout", "-b", safe_name, from_ref)
    return safe_name


def commit(message: str) -> None:
    """Stage all and commit."""
    run_git("add", "-A")
    run_git("commit", "-m", message)


def diff_stat(base: str, head: str) -> str:
    """Get diff stats for display."""
    result = run_git("diff", "--stat", f"{base}..{head}", check=False)
    return result.stdout


def diff_name_status(ref1: str, ref2: str) -> dict[str, str]:
    """Get name-status diff between refs for drift detection."""
    result = run_git("diff", "--name-status", f"{ref1}..{ref2}", check=False)

    changes: dict[str, str] = {}
    for line in result.stdout.strip().split("\n"):
        if line:
            parts = line.split("\t")
            changes[parts[-1]] = parts[0]
    return changes


def merge_base(ref1: str, ref2: str) -> str:
    """Get the merge base of two refs."""
    result = run_git("merge-base", ref1, ref2)
    return result.stdout.strip()
