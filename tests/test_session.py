"""Tests for Session class."""

import subprocess
from pathlib import Path

import pytest

from pullsaw.constants import PULLSAW_DIR
from pullsaw.session import Session


class TestSessionFromArgs:
    """Tests for Session.from_args() factory method."""

    def test_basic_creation(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)

        session = Session.from_args()

        assert session.repo_root == git_repo
        assert session.base == "main"
        assert session.head == "feature"
        assert session.config is not None
        assert session.start_from == 1
        assert not session.is_continuing

    def test_explicit_branches(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)

        session = Session.from_args(base="main", head="feature")

        assert session.base == "main"
        assert session.head == "feature"

    def test_strict_mode(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)

        session = Session.from_args(strict=True)

        assert session.config.strict is True

    def test_command_overrides(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)

        session = Session.from_args(
            test_cmd="pytest -x",
            check_cmd="mypy .",
        )

        assert session.config.test_cmd == ["pytest", "-x"]
        assert session.config.check_cmd == ["mypy", "."]

    def test_verbose_mode(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)

        session = Session.from_args(verbose=True)

        assert session.verbose is True

    def test_fails_on_main_branch(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        subprocess.run(["git", "checkout", "main"], cwd=git_repo, check=True)

        with pytest.raises(ValueError, match="Cannot run on main"):
            Session.from_args()


class TestSessionContinueMode:
    """Tests for --continue mode handling."""

    def test_continue_from_step_branch(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)

        # Create a step branch
        subprocess.run(
            ["git", "checkout", "-b", "feature-step-2"],
            cwd=git_repo,
            check=True,
        )

        session = Session.from_args(continue_run=True)

        assert session.head == "feature"
        assert session.start_from == 2
        assert session.is_continuing
        assert session._continue_info is not None
        assert session._continue_info.derived_head == "feature"

    def test_continue_fails_on_non_step_branch(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)

        with pytest.raises(ValueError, match="doesn't match pattern"):
            Session.from_args(continue_run=True)


class TestSessionSetup:
    """Tests for Session setup methods."""

    def test_setup_pullsaw_dir_creates_directory(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        session.setup_pullsaw_dir()

        pullsaw_dir = git_repo / PULLSAW_DIR
        assert pullsaw_dir.exists()
        assert pullsaw_dir.is_dir()

    def test_setup_pullsaw_dir_creates_gitignore(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        # Remove existing .gitignore
        gitignore = git_repo / ".gitignore"
        if gitignore.exists():
            gitignore.unlink()

        modified = session.setup_pullsaw_dir()

        assert modified is True
        assert gitignore.exists()
        assert PULLSAW_DIR in gitignore.read_text()

    def test_setup_pullsaw_dir_appends_to_gitignore(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        # Create existing .gitignore
        gitignore = git_repo / ".gitignore"
        gitignore.write_text("*.pyc\n__pycache__/\n")

        modified = session.setup_pullsaw_dir()

        assert modified is True
        content = gitignore.read_text()
        assert "*.pyc" in content  # Original content preserved
        assert PULLSAW_DIR in content

    def test_setup_pullsaw_dir_idempotent(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        # First call
        session.setup_pullsaw_dir()

        # Second call should not modify
        modified = session.setup_pullsaw_dir()

        assert modified is False

    def test_setup_pullsaw_dir_no_partial_match(self, git_repo: Path, monkeypatch):
        """Test that .pullsaw2 in gitignore doesn't prevent adding .pullsaw."""
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        # Create .gitignore with similar but different entry
        gitignore = git_repo / ".gitignore"
        gitignore.write_text(".pullsaw2/\n.pullsaw_backup/\n")

        modified = session.setup_pullsaw_dir()

        assert modified is True
        content = gitignore.read_text()
        # Should have both the original entries and the new .pullsaw entry
        assert ".pullsaw2/" in content
        assert ".pullsaw_backup/" in content
        assert f"/{PULLSAW_DIR}/" in content

    def test_setup_pullsaw_dir_detects_exact_entry(self, git_repo: Path, monkeypatch):
        """Test that exact .pullsaw entry is detected correctly."""
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        # Create .gitignore with the exact entry (different formats)
        for entry in [".pullsaw", ".pullsaw/", "/.pullsaw/"]:
            gitignore = git_repo / ".gitignore"
            gitignore.write_text(f"*.pyc\n{entry}\n__pycache__/\n")

            # Reset the pullsaw dir
            pullsaw_dir = git_repo / PULLSAW_DIR
            if pullsaw_dir.exists():
                import shutil

                shutil.rmtree(pullsaw_dir)

            modified = session.setup_pullsaw_dir()

            assert modified is False, f"Should not modify when '{entry}' is present"

    def test_setup_pullsaw_dir_creates_template_config(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        session.setup_pullsaw_dir()

        config_path = git_repo / PULLSAW_DIR / "config.yml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "test_cmd:" in content
        assert "max_fix_attempts:" in content

    def test_ensure_clean_working_tree_clean(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        # Should not raise
        session.ensure_clean_working_tree()

    def test_ensure_clean_working_tree_dirty(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        # Make dirty
        (git_repo / "new_file.txt").write_text("dirty")

        with pytest.raises(RuntimeError, match="uncommitted changes"):
            session.ensure_clean_working_tree()

    def test_ensure_clean_allows_gitignore(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        # Stage .gitignore change
        gitignore = git_repo / ".gitignore"
        gitignore.write_text("# new content\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=git_repo, check=True)

        # Should not raise when allow_gitignore=True
        session.ensure_clean_working_tree(allow_gitignore=True)


class TestSessionMergeBase:
    """Tests for merge_base property."""

    def test_merge_base_cached(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        # First access
        mb1 = session.merge_base
        # Second access (should be cached)
        mb2 = session.merge_base

        assert mb1 == mb2
        assert len(mb1) == 40  # Full SHA


class TestSessionChangedFiles:
    """Tests for load_changed_files method."""

    def test_load_changed_files(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        files = session.load_changed_files()

        assert "lib/foo.py" in files
        assert "lib/bar.py" in files
        assert "tests/test_foo.py" in files
        assert session.changed_files == files


class TestSessionDiffInfo:
    """Tests for get_diff_info method."""

    def test_get_diff_info(self, git_repo: Path, monkeypatch):
        monkeypatch.chdir(git_repo)
        session = Session.from_args()

        name_status, stat = session.get_diff_info()

        assert "lib/foo.py" in name_status
        assert "lib/bar.py" in name_status
        # Stat should have file count and line changes
        assert "3 files changed" in stat or "insertions" in stat
