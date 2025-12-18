"""Configuration loading and auto-detection."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    """PullSaw configuration."""

    test_cmd: list[str] = field(default_factory=lambda: ["echo", "no test command"])
    format_cmd: list[str] | None = None
    check_cmd: list[str] | None = None  # Runs after format, before tests (e.g., compile, lint)
    max_fix_attempts: int = 5
    strict: bool = False

    @classmethod
    def load(cls, repo_root: Path) -> "Config":
        """Load config from .pullsaw.yml or auto-detect."""
        config_path = repo_root / ".pullsaw.yml"

        if config_path.exists():
            return cls._from_yaml(config_path)

        return cls._auto_detect(repo_root)

    @classmethod
    def _from_yaml(cls, config_path: Path) -> "Config":
        """Load config from YAML file."""
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        return cls(
            test_cmd=data.get("test_cmd", ["echo", "no test command"]),
            format_cmd=data.get("format_cmd"),
            check_cmd=data.get("check_cmd"),
            max_fix_attempts=data.get("max_fix_attempts", 5),
            strict=data.get("strict", False),
        )

    @classmethod
    def _auto_detect(cls, repo_root: Path) -> "Config":
        """Auto-detect test/format commands based on project files."""
        # Elixir
        if (repo_root / "mix.exs").exists():
            return cls(
                test_cmd=["mix", "test", "--max-failures", "1"],
                format_cmd=["mix", "format"],
            )

        # Node.js
        if (repo_root / "package.json").exists():
            # Detect package manager
            if (repo_root / "pnpm-lock.yaml").exists():
                return cls(
                    test_cmd=["pnpm", "test"],
                    format_cmd=["pnpm", "run", "format"],
                )
            elif (repo_root / "yarn.lock").exists():
                return cls(
                    test_cmd=["yarn", "test"],
                    format_cmd=["yarn", "format"],
                )
            else:
                return cls(
                    test_cmd=["npm", "test"],
                    format_cmd=["npm", "run", "format"],
                )

        # Rust
        if (repo_root / "Cargo.toml").exists():
            return cls(
                test_cmd=["cargo", "test"],
                format_cmd=["cargo", "fmt"],
            )

        # Python
        if (repo_root / "pyproject.toml").exists() or (repo_root / "pytest.ini").exists():
            return cls(
                test_cmd=["pytest"],
                format_cmd=["ruff", "format", "."],
            )

        # Go
        if (repo_root / "go.mod").exists():
            return cls(
                test_cmd=["go", "test", "./..."],
                format_cmd=["go", "fmt", "./..."],
            )

        # Default fallback
        return cls()

