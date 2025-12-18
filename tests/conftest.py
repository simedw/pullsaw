"""Shared pytest fixtures for PullSaw tests."""

import subprocess
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a git repo with a main branch and a feature branch.

    The repo has:
    - main branch with initial README.md
    - feature branch with additional changes in lib/

    Returns:
        Path to the repo directory
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def run_git(*args: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            **kwargs,
        )

    # Initialize repo
    run_git("init")
    run_git("config", "user.email", "test@example.com")
    run_git("config", "user.name", "Test User")

    # Initial commit on main
    (repo / "README.md").write_text("# Test Project\n")
    run_git("add", ".")
    run_git("commit", "-m", "Initial commit")
    run_git("branch", "-M", "main")

    # Create feature branch with changes
    run_git("checkout", "-b", "feature")

    # Add some files
    lib_dir = repo / "lib"
    lib_dir.mkdir()
    (lib_dir / "foo.py").write_text("# foo module\ndef foo():\n    return 'foo'\n")
    (lib_dir / "bar.py").write_text("# bar module\ndef bar():\n    return 'bar'\n")

    test_dir = repo / "tests"
    test_dir.mkdir()
    (test_dir / "test_foo.py").write_text(
        "from lib.foo import foo\n\ndef test_foo():\n    assert foo() == 'foo'\n"
    )

    run_git("add", ".")
    run_git("commit", "-m", "Add lib modules and tests")

    return repo


@pytest.fixture
def simple_plan_yaml() -> str:
    """Return a simple valid plan YAML string."""
    return textwrap.dedent("""\
        stack:
          - id: 1
            title: "Add foo module"
            goal: "Introduce the foo module"
            allow:
              - "lib/foo.py"
              - "tests/test_foo.py"
            topic: "feature-foo"
          - id: 2
            title: "Add bar module"
            goal: "Introduce the bar module"
            allow:
              - "lib/bar.py"
            topic: "feature-bar"
        """)
