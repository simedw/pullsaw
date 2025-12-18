"""Tests for git operations."""

import subprocess
from pathlib import Path

import pytest

from pullsaw.git_ops import (
    FileStatus,
    GitError,
    _parse_xy_status,
    changed_files,
    current_branch,
    get_repo_root,
    infer_branches,
    is_clean,
    merge_base,
    run_git,
    sanitize_branch_name,
)


class TestGitError:
    """Tests for GitError exception."""

    def test_git_error_raised_on_failure(self, git_repo: Path, monkeypatch):
        """Test that GitError is raised with proper message when git fails."""
        monkeypatch.chdir(git_repo)

        with pytest.raises(GitError) as exc_info:
            run_git("checkout", "nonexistent-branch")

        error = exc_info.value
        assert error.returncode != 0
        assert "nonexistent-branch" in error.stderr or "did not match" in error.stderr
        assert "git checkout nonexistent-branch" in str(error)

    def test_git_error_not_raised_when_check_false(self, git_repo: Path, monkeypatch):
        """Test that GitError is not raised when check=False."""
        monkeypatch.chdir(git_repo)

        # Should not raise
        result = run_git("checkout", "nonexistent-branch", check=False)
        assert result.returncode != 0

    def test_git_error_attributes(self):
        """Test GitError attributes are set correctly."""
        error = GitError(["git", "status"], 128, "fatal: not a git repository")

        assert error.command == ["git", "status"]
        assert error.returncode == 128
        assert error.stderr == "fatal: not a git repository"
        assert "git status" in str(error)
        assert "not a git repository" in str(error)


class TestSanitizeBranchName:
    """Tests for sanitize_branch_name function."""

    @pytest.mark.parametrize(
        "input_name,expected",
        [
            # Basic cases
            ("feature-branch", "feature-branch"),
            ("my_branch", "my_branch"),
            # Spaces
            ("feature branch", "feature-branch"),
            ("a  b  c", "a-b-c"),
            # Special characters
            ("feature~1", "feature-1"),
            ("feature^2", "feature-2"),
            ("feature:name", "feature-name"),
            ("feature?name", "feature-name"),
            ("feature*name", "feature-name"),
            ("feature[name]", "feature-name"),
            ("feature\\name", "feature-name"),
            ("feature@name", "feature-name"),
            # Slashes (common in branch names)
            ("feature/branch", "feature-branch"),
            ("user/feature/name", "user-feature-name"),
            # Consecutive dots
            ("feature..branch", "feature.branch"),
            ("a...b", "a.b"),
            # Leading/trailing dots and hyphens
            (".feature", "feature"),
            ("feature.", "feature"),
            ("-feature", "feature"),
            ("feature-", "feature"),
            ("..feature..", "feature"),
            # Multiple consecutive hyphens
            ("a--b", "a-b"),
            ("a---b", "a-b"),
            # Complex combinations
            ("my feature~^:?*[]\\@/branch", "my-feature-branch"),
        ],
    )
    def test_sanitize_branch_name(self, input_name: str, expected: str):
        assert sanitize_branch_name(input_name) == expected


class TestParseXYStatus:
    """Tests for _parse_xy_status helper function."""

    def test_modified_in_index(self):
        assert _parse_xy_status("M.") == FileStatus.MODIFIED

    def test_modified_in_worktree(self):
        assert _parse_xy_status(".M") == FileStatus.MODIFIED

    def test_added(self):
        assert _parse_xy_status("A.") == FileStatus.ADDED

    def test_deleted_in_index(self):
        assert _parse_xy_status("D.") == FileStatus.DELETED

    def test_deleted_in_worktree(self):
        assert _parse_xy_status(".D") == FileStatus.DELETED

    def test_renamed(self):
        assert _parse_xy_status("R.") == FileStatus.RENAMED

    def test_copied(self):
        assert _parse_xy_status("C.") == FileStatus.COPIED

    def test_unknown_defaults_to_modified(self):
        assert _parse_xy_status("??") == FileStatus.MODIFIED


class TestGitOpsWithRepo:
    """Tests that require a real git repository."""

    def test_get_repo_root(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        root = get_repo_root()
        assert root == git_repo

    def test_current_branch(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        branch = current_branch()
        assert branch == "feature"

    def test_is_clean(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        assert is_clean()

        # Make it dirty
        (git_repo / "new_file.txt").write_text("dirty")
        assert not is_clean()

    def test_infer_branches(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        base, head = infer_branches()
        assert base == "main"
        assert head == "feature"

    def test_infer_branches_fails_on_main(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, check=True)

        with pytest.raises(ValueError, match="Cannot run on main"):
            infer_branches()

    def test_changed_files(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        files = changed_files("main", "feature")

        assert "lib/foo.py" in files
        assert "lib/bar.py" in files
        assert "tests/test_foo.py" in files
        assert files["lib/foo.py"] == FileStatus.ADDED

    def test_merge_base(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        mb = merge_base("main", "feature")
        assert len(mb) == 40  # Full SHA

        # Merge base should be the main branch tip
        main_sha = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert mb == main_sha
