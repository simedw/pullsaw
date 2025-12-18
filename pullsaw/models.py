"""Data models for PullSaw plans and steps."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .pathspec import matches_any_pattern, validate_patterns


@dataclass
class ValidationError:
    """A plan validation error."""

    message: str
    fatal: bool = True


@dataclass
class Step:
    """A single step in a stacked PR plan."""

    id: int
    title: str
    goal: str
    allow: list[str]
    shared_allow: list[str] = field(default_factory=list)
    topic: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Step":
        """Create a Step from a dictionary."""
        return cls(
            id=data.get("id", 0),
            title=data.get("title", ""),
            goal=data.get("goal", ""),
            allow=data.get("allow", []),
            shared_allow=data.get("shared_allow", []),
            topic=data.get("topic"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for YAML serialization."""
        result: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "allow": self.allow,
        }
        if self.shared_allow:
            result["shared_allow"] = self.shared_allow
        if self.topic:
            result["topic"] = self.topic
        return result

    @property
    def all_patterns(self) -> list[str]:
        """Get all patterns (allow + shared_allow)."""
        return self.allow + self.shared_allow


@dataclass
class Plan:
    """A stacked PR plan containing multiple steps."""

    steps: list[Step]
    source_file: Path | None = None

    @classmethod
    def from_yaml(cls, path: Path) -> "Plan":
        """Load a plan from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)

        if not data or "stack" not in data:
            raise ValueError(f"Invalid plan file: missing 'stack' field in {path}")

        steps = [Step.from_dict(step_data) for step_data in data["stack"]]
        return cls(steps=steps, source_file=path)

    @classmethod
    def from_dict(cls, data: dict[str, Any], source_file: Path | None = None) -> "Plan":
        """Create a Plan from a dictionary."""
        if "stack" not in data:
            raise ValueError("Invalid plan data: missing 'stack' field")

        steps = [Step.from_dict(step_data) for step_data in data["stack"]]
        return cls(steps=steps, source_file=source_file)

    def to_yaml(self) -> str:
        """Serialize the plan to YAML string."""
        data = {"stack": [step.to_dict() for step in self.steps]}
        result: str = yaml.dump(data, default_flow_style=False, sort_keys=False)
        return result

    def validate(self, changed_files: dict[str, Any]) -> list[ValidationError]:
        """Validate the plan structure and coverage.

        Checks:
        - Every step has required fields
        - Every changed file is covered by at least one pattern
        - Each step covers at least one changed file
        - No overly broad patterns

        Args:
            changed_files: Dict mapping filepath to FileStatus

        Returns:
            List of validation errors (check .fatal to distinguish warnings)
        """
        errors: list[ValidationError] = []

        if not self.steps:
            errors.append(ValidationError("Plan has no steps"))
            return errors

        # Collect all patterns from all steps
        all_patterns: list[str] = []

        for step in self.steps:
            # Check required fields
            if not step.allow:
                errors.append(ValidationError(f"Step {step.id}: missing 'allow' field"))
                continue

            if not step.title:
                errors.append(
                    ValidationError(f"Step {step.id}: missing 'title' field", fatal=False)
                )

            if not step.goal:
                errors.append(ValidationError(f"Step {step.id}: missing 'goal' field", fatal=False))

            all_patterns.extend(step.all_patterns)

            # Validate individual patterns
            pattern_errors = validate_patterns(step.all_patterns)
            for err in pattern_errors:
                errors.append(ValidationError(f"Step {step.id}: {err}"))

        # Check every changed file is covered
        uncovered: list[str] = []
        for filepath in changed_files:
            if not matches_any_pattern(filepath, all_patterns):
                uncovered.append(filepath)

        if uncovered:
            errors.append(ValidationError(f"Files not covered by any step: {uncovered}"))

        # Check each step covers at least one file
        for step in self.steps:
            covers_any = any(
                matches_any_pattern(filepath, step.all_patterns) for filepath in changed_files
            )

            if not covers_any:
                errors.append(
                    ValidationError(f"Step {step.id} doesn't cover any changed files", fatal=False)
                )

        return errors

    def get_step(self, step_id: int) -> Step | None:
        """Get a step by ID."""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def __len__(self) -> int:
        return len(self.steps)

    def __iter__(self) -> Iterator[Step]:
        return iter(self.steps)
