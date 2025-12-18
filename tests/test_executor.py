"""Tests for the executor module."""

import subprocess
from unittest.mock import patch

import pytest

from pullsaw.claude_code import ClaudeResult
from pullsaw.config import Config
from pullsaw.executor import (
    check_allowlist,
    execute,
    execute_step,
    run_command,
)
from pullsaw.models import Plan, Step


class TestRunCommand:
    """Tests for run_command function."""

    def test_successful_command(self):
        result = run_command(["echo", "hello"], timeout=10)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_failed_command(self):
        result = run_command(["false"], timeout=10)
        assert result.returncode != 0

    def test_timeout_returns_exit_124(self):
        result = run_command(["sleep", "10"], timeout=1)
        assert result.returncode == 124
        assert "timed out" in result.stderr


class TestCheckAllowlist:
    """Tests for check_allowlist function."""

    @patch("pullsaw.executor.git_ops.changed_files_working")
    def test_returns_files_outside_allowlist(self, mock_changed):
        from pullsaw.git_ops import FileStatus

        mock_changed.return_value = {
            "lib/foo.py": FileStatus.MODIFIED,
            "lib/bar.py": FileStatus.MODIFIED,
            "lib/baz.py": FileStatus.MODIFIED,
        }

        outside = check_allowlist(["lib/foo.py", "lib/bar.py"])

        assert outside == ["lib/baz.py"]

    @patch("pullsaw.executor.git_ops.changed_files_working")
    def test_allows_pullsaw_files(self, mock_changed):
        from pullsaw.git_ops import FileStatus

        mock_changed.return_value = {
            ".gitignore": FileStatus.MODIFIED,
            ".pullsaw/config.yml": FileStatus.MODIFIED,
            "lib/foo.py": FileStatus.MODIFIED,
        }

        outside = check_allowlist(["lib/foo.py"])

        # .gitignore and .pullsaw/* should not be reported
        assert outside == []

    @patch("pullsaw.executor.git_ops.changed_files_working")
    def test_pattern_matching(self, mock_changed):
        from pullsaw.git_ops import FileStatus

        mock_changed.return_value = {
            "lib/auth/user.py": FileStatus.MODIFIED,
            "lib/auth/session.py": FileStatus.MODIFIED,
            "lib/other/file.py": FileStatus.MODIFIED,
        }

        outside = check_allowlist(["lib/auth/**"])

        assert outside == ["lib/other/file.py"]


class TestExecuteStep:
    """Tests for execute_step function with mocked dependencies."""

    @pytest.fixture
    def basic_step(self) -> Step:
        return Step(
            id=1,
            title="Add feature",
            goal="Implement the feature",
            allow=["lib/feature/**"],
            topic="my-feature",
        )

    @pytest.fixture
    def basic_config(self) -> Config:
        return Config(
            test_cmd=["echo", "tests pass"],
            format_cmd=["echo", "formatted"],
            max_fix_attempts=3,
        )

    @patch("pullsaw.executor.git_ops")
    @patch("pullsaw.executor.claude_code")
    @patch("pullsaw.executor.run_command_streaming")
    def test_execute_step_success(
        self, mock_run_streaming, mock_claude, mock_git, basic_step, basic_config
    ):
        """Test successful step execution."""
        # Setup mocks
        mock_git.create_branch.return_value = "feature-step-1"
        mock_git.changed_files_working.return_value = {}

        mock_claude.implement_step.return_value = ClaudeResult(
            success=True,
            session_id="session-123",
            output="Done",
            error=None,
            raw={"total_cost_usd": 0.05},
        )

        # Mock run_command_streaming for format and test commands
        mock_run_streaming.return_value = subprocess.CompletedProcess(
            ["echo"], returncode=0, stdout="OK", stderr=""
        )

        branch, topic, cost = execute_step(basic_step, "main", "feature", basic_config)

        assert branch == "feature-step-1"
        assert topic == "my-feature"
        assert cost == pytest.approx(0.05)

        # Verify branch was created
        mock_git.create_branch.assert_called_once_with("feature-step-1", "main")

        # Verify commit was made
        mock_git.commit.assert_called_once()
        commit_msg = mock_git.commit.call_args[0][0]
        assert "step(1)" in commit_msg
        assert "Add feature" in commit_msg
        assert "Topic: my-feature" in commit_msg

    @patch("pullsaw.executor.git_ops")
    @patch("pullsaw.executor.claude_code")
    @patch("pullsaw.executor.run_command_streaming")
    def test_execute_step_with_fix_attempt(
        self, mock_run_streaming, mock_claude, mock_git, basic_step, basic_config
    ):
        """Test step execution with a failed test that gets fixed."""
        mock_git.create_branch.return_value = "feature-step-1"
        mock_git.changed_files_working.return_value = {}

        mock_claude.implement_step.return_value = ClaudeResult(
            success=True,
            session_id="session-123",
            output="Done",
            error=None,
            raw={"total_cost_usd": 0.05},
        )

        mock_claude.fix_failures.return_value = ClaudeResult(
            success=True,
            session_id="session-123",
            output="Fixed",
            error=None,
            raw={"total_cost_usd": 0.03},
        )

        # First test fails, second succeeds
        call_count = [0]

        def mock_run_side_effect(cmd, timeout):
            call_count[0] += 1
            # Format always passes
            if cmd == basic_config.format_cmd:
                return subprocess.CompletedProcess(cmd, 0, "OK", "")
            # Test: fail first time, pass second time
            if cmd == basic_config.test_cmd:
                if call_count[0] <= 2:  # First test run (after format)
                    return subprocess.CompletedProcess(cmd, 1, "FAIL", "")
                return subprocess.CompletedProcess(cmd, 0, "PASS", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        mock_run_streaming.side_effect = mock_run_side_effect

        branch, topic, cost = execute_step(basic_step, "main", "feature", basic_config)

        # Should have called fix_failures
        mock_claude.fix_failures.assert_called_once()

        # Cost should include both implementation and fix
        assert cost == pytest.approx(0.08)

    @patch("pullsaw.executor.git_ops")
    @patch("pullsaw.executor.claude_code")
    @patch("pullsaw.executor.run_command_streaming")
    def test_execute_step_max_attempts_exceeded(
        self, mock_run_streaming, mock_claude, mock_git, basic_step, basic_config
    ):
        """Test step execution fails after max fix attempts."""
        mock_git.create_branch.return_value = "feature-step-1"
        mock_git.changed_files_working.return_value = {}

        mock_claude.implement_step.return_value = ClaudeResult(
            success=True,
            session_id="session-123",
            output="Done",
            error=None,
            raw={},
        )

        mock_claude.fix_failures.return_value = ClaudeResult(
            success=True,
            session_id="session-123",
            output="Tried",
            error=None,
            raw={},
        )

        # All tests fail
        def mock_run_side_effect(cmd, timeout):
            if cmd == basic_config.format_cmd:
                return subprocess.CompletedProcess(cmd, 0, "OK", "")
            if cmd == basic_config.test_cmd:
                return subprocess.CompletedProcess(cmd, 1, "FAIL", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        mock_run_streaming.side_effect = mock_run_side_effect

        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            execute_step(basic_step, "main", "feature", basic_config)

    @patch("pullsaw.executor.git_ops")
    @patch("pullsaw.executor.claude_code")
    def test_execute_step_retry_mode(self, mock_claude, mock_git, basic_step, basic_config):
        """Test retry mode skips implementation."""
        mock_git.changed_files_working.return_value = {}

        # Mock test passing
        with patch("pullsaw.executor.run_command_streaming") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["echo"], returncode=0, stdout="OK", stderr=""
            )

            branch, topic, cost = execute_step(
                basic_step, "main", "feature", basic_config, is_retry=True
            )

        # Should NOT call implement_step in retry mode
        mock_claude.implement_step.assert_not_called()

        # Should NOT create branch in retry mode
        mock_git.create_branch.assert_not_called()


class TestExecute:
    """Tests for the execute function."""

    @pytest.fixture
    def simple_plan(self) -> Plan:
        return Plan(
            steps=[
                Step(id=1, title="Step 1", goal="Goal 1", allow=["lib/a/**"]),
                Step(id=2, title="Step 2", goal="Goal 2", allow=["lib/b/**"]),
            ]
        )

    @pytest.fixture
    def basic_config(self) -> Config:
        return Config(
            test_cmd=["echo", "pass"],
            max_fix_attempts=1,
        )

    @patch("pullsaw.executor.execute_step")
    @patch("pullsaw.executor.git_ops.diff_name_status")
    def test_execute_all_steps(self, mock_diff, mock_execute_step, simple_plan, basic_config):
        """Test executing all steps in a plan."""
        mock_execute_step.side_effect = [
            ("feature-step-1", "topic-1", 0.05),
            ("feature-step-2", "topic-2", 0.03),
        ]
        mock_diff.return_value = {}  # No drift

        branches = execute(simple_plan, "main", "feature", basic_config)

        assert branches == ["feature-step-1", "feature-step-2"]
        assert mock_execute_step.call_count == 2

    @patch("pullsaw.executor.execute_step")
    @patch("pullsaw.executor.git_ops.diff_name_status")
    def test_execute_with_start_from(self, mock_diff, mock_execute_step, simple_plan, basic_config):
        """Test continuing from a specific step."""
        mock_execute_step.return_value = ("feature-step-2", "topic-2", 0.03)
        mock_diff.return_value = {}

        branches = execute(simple_plan, "main", "feature", basic_config, start_from=2)

        # Should include step 1 branch (already done) + step 2 (executed)
        assert len(branches) == 2
        assert branches[0] == "feature-step-1"  # Already done
        assert branches[1] == "feature-step-2"  # Newly executed

        # Only step 2 should be executed
        assert mock_execute_step.call_count == 1

    @patch("pullsaw.executor.execute_step")
    @patch("pullsaw.executor.git_ops.diff_name_status")
    def test_execute_with_skip(self, mock_diff, mock_execute_step, simple_plan, basic_config):
        """Test skipping a step."""
        mock_diff.return_value = {}

        # With skip_current=True, step 2 (start_from=2) should be skipped
        branches = execute(
            simple_plan, "main", "feature", basic_config, start_from=2, skip_current=True
        )

        # Both steps should be in branches but neither executed
        assert len(branches) == 2
        mock_execute_step.assert_not_called()

    @patch("pullsaw.executor.execute_step")
    @patch("pullsaw.executor.git_ops.diff_name_status")
    def test_execute_with_drift_strict_mode(
        self, mock_diff, mock_execute_step, simple_plan, basic_config
    ):
        """Test drift detection in strict mode."""
        basic_config.strict = True

        mock_execute_step.side_effect = [
            ("feature-step-1", "topic-1", 0.05),
            ("feature-step-2", "topic-2", 0.03),
        ]
        mock_diff.return_value = {"lib/extra.py": "A"}  # Drift detected

        with pytest.raises(RuntimeError, match="Drift detected"):
            execute(simple_plan, "main", "feature", basic_config)

    @patch("pullsaw.executor.execute_step")
    @patch("pullsaw.executor.git_ops.diff_name_status")
    def test_execute_without_drift_strict_mode(
        self, mock_diff, mock_execute_step, simple_plan, basic_config
    ):
        """Test no drift in strict mode passes."""
        basic_config.strict = True

        mock_execute_step.side_effect = [
            ("feature-step-1", "topic-1", 0.05),
            ("feature-step-2", "topic-2", 0.03),
        ]
        mock_diff.return_value = {}  # No drift

        # Should not raise
        branches = execute(simple_plan, "main", "feature", basic_config)
        assert len(branches) == 2
