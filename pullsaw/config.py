"""Configuration loading and auto-detection."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigValidationError(Exception):
    """Raised when config.yml has invalid values."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        message = "Invalid config.yml:\n" + "\n".join(f"  - {e}" for e in errors)
        super().__init__(message)


# Schema definition for validation
CONFIG_SCHEMA: dict[str, dict[str, Any]] = {
    "test_cmd": {"type": list, "item_type": str, "required": False},
    "format_cmd": {"type": list, "item_type": str, "required": False, "nullable": True},
    "check_cmd": {"type": list, "item_type": str, "required": False, "nullable": True},
    "max_fix_attempts": {"type": int, "min": 1, "max": 20, "required": False},
    "strict": {"type": bool, "required": False},
    "test_timeout": {"type": int, "min": 10, "max": 3600, "required": False},
    "command_timeout": {"type": int, "min": 10, "max": 3600, "required": False},
}


def validate_config_data(data: dict[str, Any]) -> list[str]:
    """Validate config data against the schema.

    Returns a list of error messages (empty if valid).
    """
    errors: list[str] = []

    # Check for unknown keys
    known_keys = set(CONFIG_SCHEMA.keys())
    for key in data:
        if key not in known_keys:
            errors.append(f"Unknown config key: '{key}'")

    # Validate each field
    for key, schema in CONFIG_SCHEMA.items():
        if key not in data:
            continue

        value = data[key]

        # Handle nullable fields
        if value is None:
            if schema.get("nullable"):
                continue
            errors.append(f"'{key}' cannot be null")
            continue

        # Type check
        expected_type = schema["type"]
        if not isinstance(value, expected_type):
            errors.append(f"'{key}' must be {expected_type.__name__}, got {type(value).__name__}")
            continue

        # List item type check
        if expected_type is list and "item_type" in schema:
            item_type = schema["item_type"]
            for i, item in enumerate(value):
                if not isinstance(item, item_type):
                    errors.append(
                        f"'{key}[{i}]' must be {item_type.__name__}, got {type(item).__name__}"
                    )

        # Range checks for int
        if expected_type is int:
            if "min" in schema and value < schema["min"]:
                errors.append(f"'{key}' must be >= {schema['min']}, got {value}")
            if "max" in schema and value > schema["max"]:
                errors.append(f"'{key}' must be <= {schema['max']}, got {value}")

    return errors


@dataclass
class Config:
    """PullSaw configuration."""

    test_cmd: list[str] = field(default_factory=lambda: ["echo", "no test command"])
    format_cmd: list[str] | None = None
    check_cmd: list[str] | None = None  # Runs after format, before tests (e.g., compile, lint)
    max_fix_attempts: int = 5
    strict: bool = False
    test_timeout: int = 300  # Timeout for test commands (seconds)
    command_timeout: int = 120  # Timeout for other commands (seconds)

    @classmethod
    def load(cls, repo_root: Path) -> "Config":
        """Load config from .pullsaw/config.yml or auto-detect."""
        config_path = repo_root / ".pullsaw" / "config.yml"
        if config_path.exists():
            return cls._from_yaml(config_path)

        return cls._auto_detect(repo_root)

    @classmethod
    def _from_yaml(cls, config_path: Path) -> "Config":
        """Load config from YAML file.

        Raises:
            ConfigValidationError: If the config has invalid values
            yaml.YAMLError: If the YAML is malformed
        """
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        # Validate the config data
        errors = validate_config_data(data)
        if errors:
            raise ConfigValidationError(errors)

        return cls(
            test_cmd=data.get("test_cmd", ["echo", "no test command"]),
            format_cmd=data.get("format_cmd"),
            check_cmd=data.get("check_cmd"),
            max_fix_attempts=data.get("max_fix_attempts", 5),
            strict=data.get("strict", False),
            test_timeout=data.get("test_timeout", 300),
            command_timeout=data.get("command_timeout", 120),
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

    @classmethod
    def generate_template(cls, repo_root: Path) -> str:
        """Generate a template config.yml with auto-detected values."""
        detected = cls._auto_detect(repo_root)

        lines = [
            "# PullSaw configuration",
            "# See: https://github.com/simedw/pullsaw#configuration",
            "",
        ]

        # Test command
        test_cmd_str = str(detected.test_cmd).replace("'", '"')
        lines.append(f"test_cmd: {test_cmd_str}")

        # Format command
        if detected.format_cmd:
            format_cmd_str = str(detected.format_cmd).replace("'", '"')
            lines.append(f"format_cmd: {format_cmd_str}")
        else:
            lines.append("# format_cmd: []")

        # Check command
        lines.append("")
        lines.append("# Optional: runs after format, before tests (e.g., compile, typecheck)")
        if detected.check_cmd:
            check_cmd_str = str(detected.check_cmd).replace("'", '"')
            lines.append(f"check_cmd: {check_cmd_str}")
        else:
            lines.append("# check_cmd: []")

        # Other options
        lines.append("")
        lines.append("# Max attempts to fix failing tests")
        lines.append(f"max_fix_attempts: {detected.max_fix_attempts}")
        lines.append("")
        lines.append("# Timeout for test commands (seconds)")
        lines.append(f"test_timeout: {detected.test_timeout}")
        lines.append("")
        lines.append("# Timeout for other commands like format/check (seconds)")
        lines.append(f"command_timeout: {detected.command_timeout}")
        lines.append("")
        lines.append("# Fail if final state doesn't match original branch")
        lines.append(f"strict: {str(detected.strict).lower()}")

        return "\n".join(lines) + "\n"
