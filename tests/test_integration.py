"""Integration tests for PullSaw.

These tests use real git repositories in temp directories.
"""

import subprocess
from pathlib import Path

import pytest

from pullsaw import git_ops
from pullsaw.config import Config, ConfigValidationError, validate_config_data
from pullsaw.models import Plan, Step


class TestGitOpsIntegration:
    """Integration tests for git operations with real repos."""

    def test_full_workflow(self, git_repo: Path, monkeypatch):
        """Test a full git workflow: branch, commit, diff."""
        monkeypatch.chdir(git_repo)

        # Get initial state
        base = "main"
        head = "feature"

        # Check branches are correct
        inferred_base, inferred_head = git_ops.infer_branches()
        assert inferred_base == base
        assert inferred_head == head

        # Get changed files
        files = git_ops.changed_files(base, head)
        assert len(files) == 3  # foo.py, bar.py, test_foo.py

        # Get diff stat
        stat = git_ops.diff_stat(base, head)
        assert "3 files changed" in stat

    def test_create_branch_and_commit(self, git_repo: Path, monkeypatch):
        """Test creating a branch and committing."""
        monkeypatch.chdir(git_repo)

        # Create a new branch
        new_branch = git_ops.create_branch("test-branch", "feature")
        assert new_branch == "test-branch"
        assert git_ops.current_branch() == "test-branch"

        # Make a change
        (git_repo / "lib" / "new_file.py").write_text("# new\n")

        # Commit
        git_ops.commit("Add new file")

        # Verify commit
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Add new file" in result.stdout

    def test_drift_detection(self, git_repo: Path, monkeypatch):
        """Test detecting drift between branches."""
        monkeypatch.chdir(git_repo)

        # Create divergent branches
        subprocess.run(["git", "checkout", "-b", "branch-a", "main"], cwd=git_repo, check=True)
        (git_repo / "file_a.txt").write_text("a")
        subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "add a"], cwd=git_repo, check=True)

        subprocess.run(["git", "checkout", "-b", "branch-b", "main"], cwd=git_repo, check=True)
        (git_repo / "file_b.txt").write_text("b")
        subprocess.run(["git", "add", "."], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "add b"], cwd=git_repo, check=True)

        # Check drift
        drift = git_ops.diff_name_status("branch-a", "branch-b")
        assert "file_a.txt" in drift  # Missing in branch-b
        assert "file_b.txt" in drift  # Added in branch-b


class TestConfigIntegration:
    """Integration tests for config auto-detection."""

    def test_auto_detect_python(self, git_repo: Path, monkeypatch):
        """Test auto-detection of Python project."""
        monkeypatch.chdir(git_repo)

        # Create pyproject.toml
        (git_repo / "pyproject.toml").write_text('[project]\nname = "test"\n')

        config = Config.load(git_repo)

        assert config.test_cmd == ["pytest"]
        assert config.format_cmd == ["ruff", "format", "."]

    def test_auto_detect_elixir(self, git_repo: Path, monkeypatch):
        """Test auto-detection of Elixir project."""
        monkeypatch.chdir(git_repo)

        # Create mix.exs
        (git_repo / "mix.exs").write_text("defmodule Test.MixProject do\nend\n")

        config = Config.load(git_repo)

        assert config.test_cmd == ["mix", "test", "--max-failures", "1"]
        assert config.format_cmd == ["mix", "format"]

    def test_config_from_yaml(self, git_repo: Path, monkeypatch):
        """Test loading config from .pullsaw/config.yml."""
        monkeypatch.chdir(git_repo)

        # Create config file in .pullsaw directory
        pullsaw_dir = git_repo / ".pullsaw"
        pullsaw_dir.mkdir()
        (pullsaw_dir / "config.yml").write_text(
            """test_cmd: ["pytest", "-x"]
format_cmd: ["black", "."]
check_cmd: ["mypy", "."]
max_fix_attempts: 3
strict: true
"""
        )

        config = Config.load(git_repo)

        assert config.test_cmd == ["pytest", "-x"]
        assert config.format_cmd == ["black", "."]
        assert config.check_cmd == ["mypy", "."]
        assert config.max_fix_attempts == 3
        assert config.strict is True

    def test_generate_template(self, git_repo: Path, monkeypatch):
        """Test generating a template config file."""
        monkeypatch.chdir(git_repo)

        # Create pyproject.toml to trigger Python detection
        (git_repo / "pyproject.toml").write_text('[project]\nname = "test"\n')

        template = Config.generate_template(git_repo)

        assert "test_cmd:" in template
        assert "pytest" in template
        assert "format_cmd:" in template
        assert "ruff" in template
        assert "max_fix_attempts:" in template
        assert "strict:" in template


class TestPlanIntegration:
    """Integration tests for plan loading and validation."""

    def test_plan_from_yaml_and_validate(self, git_repo: Path, monkeypatch, simple_plan_yaml: str):
        """Test loading a plan and validating against real changed files."""
        monkeypatch.chdir(git_repo)

        # Write plan file
        plan_file = git_repo / ".pullsaw" / "plan.yaml"
        plan_file.parent.mkdir(exist_ok=True)
        plan_file.write_text(simple_plan_yaml)

        # Load plan
        plan = Plan.from_yaml(plan_file)
        assert len(plan.steps) == 2

        # Get changed files
        changed_files = git_ops.changed_files("main", "feature")

        # Validate - should have error because bar.py is not in tests
        errors = plan.validate(changed_files)

        # The simple_plan_yaml doesn't cover tests/test_foo.py, so there should be an error
        # Actually looking at the fixture, it does cover tests/test_foo.py in step 1
        # But lib/bar.py in step 2 doesn't have tests - but that's OK since we only
        # check coverage, not test coverage
        fatal_errors = [e for e in errors if e.fatal]

        # All files should be covered
        assert not fatal_errors

    def test_plan_round_trip(self, tmp_path: Path):
        """Test that a plan can be serialized and deserialized."""
        plan = Plan(
            steps=[
                Step(
                    id=1,
                    title="Step 1",
                    goal="Do step 1",
                    allow=["lib/foo/**"],
                    topic="topic-1",
                ),
                Step(
                    id=2,
                    title="Step 2",
                    goal="Do step 2",
                    allow=["lib/bar/**"],
                    shared_allow=["config/**"],
                    topic="topic-2",
                ),
            ]
        )

        # Serialize
        yaml_str = plan.to_yaml()

        # Write and read back
        plan_file = tmp_path / "plan.yaml"
        plan_file.write_text(yaml_str)
        loaded = Plan.from_yaml(plan_file)

        # Verify
        assert len(loaded.steps) == 2
        assert loaded.steps[0].title == "Step 1"
        assert loaded.steps[0].topic == "topic-1"
        assert loaded.steps[1].shared_allow == ["config/**"]


class TestConfigValidation:
    """Tests for config.yml schema validation."""

    def test_valid_config(self):
        """Test that valid config data passes validation."""
        data = {
            "test_cmd": ["pytest", "-x"],
            "format_cmd": ["ruff", "format", "."],
            "check_cmd": ["mypy", "."],
            "max_fix_attempts": 5,
            "strict": True,
            "test_timeout": 300,
            "command_timeout": 120,
        }

        errors = validate_config_data(data)
        assert errors == []

    def test_unknown_key(self):
        """Test that unknown keys are reported."""
        data = {"unknown_key": "value", "another_unknown": 123}

        errors = validate_config_data(data)

        assert len(errors) == 2
        assert any("unknown_key" in e for e in errors)
        assert any("another_unknown" in e for e in errors)

    def test_invalid_type_test_cmd(self):
        """Test that test_cmd must be a list."""
        data = {"test_cmd": "pytest"}  # Should be a list

        errors = validate_config_data(data)

        assert len(errors) == 1
        assert "test_cmd" in errors[0]
        assert "list" in errors[0]

    def test_invalid_list_item_type(self):
        """Test that list items must be strings."""
        data = {"test_cmd": ["pytest", 123, True]}  # Items should be strings

        errors = validate_config_data(data)

        assert len(errors) == 2  # Two invalid items
        assert any("[1]" in e for e in errors)  # Index 1
        assert any("[2]" in e for e in errors)  # Index 2

    def test_max_fix_attempts_range(self):
        """Test max_fix_attempts range validation."""
        # Too low
        errors = validate_config_data({"max_fix_attempts": 0})
        assert any(">= 1" in e for e in errors)

        # Too high
        errors = validate_config_data({"max_fix_attempts": 100})
        assert any("<= 20" in e for e in errors)

        # Valid
        errors = validate_config_data({"max_fix_attempts": 10})
        assert errors == []

    def test_timeout_range(self):
        """Test timeout range validation."""
        # Too low
        errors = validate_config_data({"test_timeout": 5})
        assert any(">= 10" in e for e in errors)

        # Too high
        errors = validate_config_data({"test_timeout": 10000})
        assert any("<= 3600" in e for e in errors)

    def test_strict_must_be_bool(self):
        """Test strict must be boolean."""
        data = {"strict": "yes"}  # Should be bool

        errors = validate_config_data(data)

        assert len(errors) == 1
        assert "bool" in errors[0]

    def test_nullable_fields(self):
        """Test that nullable fields accept null."""
        data = {"format_cmd": None, "check_cmd": None}

        errors = validate_config_data(data)

        assert errors == []

    def test_config_validation_error_raised(self, git_repo: Path, monkeypatch):
        """Test that ConfigValidationError is raised for invalid config."""
        monkeypatch.chdir(git_repo)

        pullsaw_dir = git_repo / ".pullsaw"
        pullsaw_dir.mkdir()
        (pullsaw_dir / "config.yml").write_text(
            """test_cmd: "not a list"
max_fix_attempts: 0
unknown_option: true
"""
        )

        with pytest.raises(ConfigValidationError) as exc_info:
            Config.load(git_repo)

        error = exc_info.value
        assert len(error.errors) == 3
        assert any("test_cmd" in e for e in error.errors)
        assert any("max_fix_attempts" in e for e in error.errors)
        assert any("unknown_option" in e for e in error.errors)
