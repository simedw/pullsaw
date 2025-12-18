"""Tests for Plan and Step models."""

from pathlib import Path

import pytest

from pullsaw.git_ops import FileStatus
from pullsaw.models import Plan, Step


class TestStep:
    """Tests for Step dataclass."""

    def test_from_dict_minimal(self):
        data = {
            "id": 1,
            "title": "Test Step",
            "goal": "Do something",
            "allow": ["lib/**"],
        }
        step = Step.from_dict(data)

        assert step.id == 1
        assert step.title == "Test Step"
        assert step.goal == "Do something"
        assert step.allow == ["lib/**"]
        assert step.shared_allow == []
        assert step.topic is None

    def test_from_dict_full(self):
        data = {
            "id": 2,
            "title": "Full Step",
            "goal": "Do everything",
            "allow": ["lib/foo/**"],
            "shared_allow": ["config/**"],
            "topic": "my-topic",
        }
        step = Step.from_dict(data)

        assert step.id == 2
        assert step.allow == ["lib/foo/**"]
        assert step.shared_allow == ["config/**"]
        assert step.topic == "my-topic"

    def test_to_dict(self):
        step = Step(
            id=1,
            title="Test",
            goal="Goal",
            allow=["lib/**"],
            shared_allow=["config/**"],
            topic="topic-1",
        )
        data = step.to_dict()

        assert data["id"] == 1
        assert data["title"] == "Test"
        assert data["allow"] == ["lib/**"]
        assert data["shared_allow"] == ["config/**"]
        assert data["topic"] == "topic-1"

    def test_to_dict_omits_empty_optional_fields(self):
        step = Step(
            id=1,
            title="Test",
            goal="Goal",
            allow=["lib/**"],
        )
        data = step.to_dict()

        assert "shared_allow" not in data
        assert "topic" not in data

    def test_all_patterns(self):
        step = Step(
            id=1,
            title="Test",
            goal="Goal",
            allow=["lib/**"],
            shared_allow=["config/**"],
        )
        assert step.all_patterns == ["lib/**", "config/**"]


class TestPlan:
    """Tests for Plan dataclass."""

    def test_from_yaml(self, tmp_path: Path, simple_plan_yaml: str):
        plan_file = tmp_path / "plan.yaml"
        plan_file.write_text(simple_plan_yaml)

        plan = Plan.from_yaml(plan_file)

        assert len(plan.steps) == 2
        assert plan.steps[0].id == 1
        assert plan.steps[0].title == "Add foo module"
        assert plan.steps[1].id == 2
        assert plan.source_file == plan_file

    def test_from_yaml_missing_stack(self, tmp_path: Path):
        plan_file = tmp_path / "plan.yaml"
        plan_file.write_text("invalid: data\n")

        with pytest.raises(ValueError, match="missing 'stack' field"):
            Plan.from_yaml(plan_file)

    def test_from_dict(self):
        data = {
            "stack": [
                {"id": 1, "title": "Step 1", "goal": "Goal 1", "allow": ["lib/**"]},
                {"id": 2, "title": "Step 2", "goal": "Goal 2", "allow": ["test/**"]},
            ]
        }
        plan = Plan.from_dict(data)

        assert len(plan.steps) == 2
        assert plan.source_file is None

    def test_to_yaml(self):
        plan = Plan(
            steps=[
                Step(id=1, title="Test", goal="Goal", allow=["lib/**"]),
            ]
        )
        yaml_str = plan.to_yaml()

        assert "stack:" in yaml_str
        assert "id: 1" in yaml_str
        assert "title: Test" in yaml_str

    def test_get_step(self):
        plan = Plan(
            steps=[
                Step(id=1, title="Step 1", goal="Goal", allow=["lib/**"]),
                Step(id=2, title="Step 2", goal="Goal", allow=["test/**"]),
            ]
        )

        assert plan.get_step(1).title == "Step 1"
        assert plan.get_step(2).title == "Step 2"
        assert plan.get_step(3) is None

    def test_len(self):
        plan = Plan(
            steps=[Step(id=i, title=f"Step {i}", goal="Goal", allow=["lib/**"]) for i in range(3)]
        )
        assert len(plan) == 3

    def test_iter(self):
        steps = [Step(id=i, title=f"Step {i}", goal="Goal", allow=["lib/**"]) for i in range(3)]
        plan = Plan(steps=steps)

        for i, step in enumerate(plan):
            assert step == steps[i]


class TestPlanValidation:
    """Tests for Plan.validate() method."""

    def test_valid_plan(self):
        plan = Plan(
            steps=[
                Step(id=1, title="Step 1", goal="Goal", allow=["lib/foo.py"]),
                Step(id=2, title="Step 2", goal="Goal", allow=["lib/bar.py"]),
            ]
        )
        changed_files = {
            "lib/foo.py": FileStatus.ADDED,
            "lib/bar.py": FileStatus.ADDED,
        }

        errors = plan.validate(changed_files)
        assert not any(e.fatal for e in errors)

    def test_empty_plan(self):
        plan = Plan(steps=[])
        errors = plan.validate({})

        assert len(errors) == 1
        assert errors[0].fatal
        assert "no steps" in errors[0].message

    def test_missing_allow_field(self):
        plan = Plan(
            steps=[
                Step(id=1, title="Step 1", goal="Goal", allow=[]),
            ]
        )

        errors = plan.validate({"lib/foo.py": FileStatus.ADDED})
        fatal_errors = [e for e in errors if e.fatal]

        assert len(fatal_errors) >= 1
        assert any("missing 'allow'" in e.message for e in fatal_errors)

    def test_missing_title_is_warning(self):
        plan = Plan(
            steps=[
                Step(id=1, title="", goal="Goal", allow=["lib/**"]),
            ]
        )

        errors = plan.validate({"lib/foo.py": FileStatus.ADDED})
        warnings = [e for e in errors if not e.fatal]

        assert any("missing 'title'" in e.message for e in warnings)

    def test_missing_goal_is_warning(self):
        plan = Plan(
            steps=[
                Step(id=1, title="Title", goal="", allow=["lib/**"]),
            ]
        )

        errors = plan.validate({"lib/foo.py": FileStatus.ADDED})
        warnings = [e for e in errors if not e.fatal]

        assert any("missing 'goal'" in e.message for e in warnings)

    def test_uncovered_files(self):
        plan = Plan(
            steps=[
                Step(id=1, title="Step 1", goal="Goal", allow=["lib/foo.py"]),
            ]
        )
        changed_files = {
            "lib/foo.py": FileStatus.ADDED,
            "lib/bar.py": FileStatus.ADDED,  # Not covered!
        }

        errors = plan.validate(changed_files)
        fatal_errors = [e for e in errors if e.fatal]

        assert any("not covered" in e.message for e in fatal_errors)
        assert any("lib/bar.py" in e.message for e in fatal_errors)

    def test_step_covers_no_files_is_warning(self):
        plan = Plan(
            steps=[
                Step(id=1, title="Step 1", goal="Goal", allow=["lib/**"]),
                Step(id=2, title="Step 2", goal="Goal", allow=["nonexistent/**"]),
            ]
        )
        changed_files = {
            "lib/foo.py": FileStatus.ADDED,
        }

        errors = plan.validate(changed_files)
        warnings = [e for e in errors if not e.fatal]

        assert any("Step 2" in e.message and "doesn't cover" in e.message for e in warnings)

    def test_too_broad_pattern(self):
        plan = Plan(
            steps=[
                Step(id=1, title="Step 1", goal="Goal", allow=["**"]),
            ]
        )

        errors = plan.validate({"lib/foo.py": FileStatus.ADDED})
        fatal_errors = [e for e in errors if e.fatal]

        assert any("too broad" in e.message for e in fatal_errors)
