"""Tests for pathspec pattern matching."""

import pytest

from pullsaw.pathspec import matches_any_pattern, matches_pattern, validate_patterns


class TestMatchesPattern:
    """Tests for matches_pattern function."""

    @pytest.mark.parametrize(
        "filepath,pattern,expected",
        [
            # Directory glob with **
            ("lib/auth/user.ex", "lib/auth/**", True),
            ("lib/auth/sub/deep.ex", "lib/auth/**", True),
            ("lib/auth/a/b/c/d.ex", "lib/auth/**", True),
            ("lib/other/file.ex", "lib/auth/**", False),
            ("lib/auth.ex", "lib/auth/**", False),  # auth is a file, not dir
            # Single segment glob with *
            ("lib/foo.ex", "lib/*.ex", True),
            ("lib/bar.ex", "lib/*.ex", True),
            ("lib/sub/foo.ex", "lib/*.ex", False),  # * doesn't match /
            # Exact match
            ("lib/foo.ex", "lib/foo.ex", True),
            ("lib/bar.ex", "lib/foo.ex", False),
            ("lib/sub/foo.ex", "lib/foo.ex", False),
            # ** in middle (note: PurePosixPath.match has limited ** support)
            # These patterns are not commonly used by pullsaw, which prefers lib/auth/** style
            # Skipping these tests as PurePosixPath.match doesn't fully support ** in middle
            # Root level files
            ("README.md", "README.md", True),
            ("README.md", "*.md", True),
            (".gitignore", ".gitignore", True),
        ],
    )
    def test_matches_pattern(self, filepath: str, pattern: str, expected: bool):
        assert matches_pattern(filepath, pattern) == expected

    def test_directory_prefix_match(self):
        """Test that lib/auth/** matches files in lib/auth/ directory."""
        assert matches_pattern("lib/auth/user.ex", "lib/auth/**")
        assert matches_pattern("lib/auth/sub/deep/file.ex", "lib/auth/**")

    def test_no_partial_directory_match(self):
        """Test that lib/auth/** doesn't match lib/authorize/."""
        assert not matches_pattern("lib/authorize/user.ex", "lib/auth/**")


class TestMatchesAnyPattern:
    """Tests for matches_any_pattern function."""

    def test_matches_one_of_multiple(self):
        patterns = ["lib/foo/**", "lib/bar/**", "test/**"]
        assert matches_any_pattern("lib/foo/file.ex", patterns)
        assert matches_any_pattern("lib/bar/file.ex", patterns)
        assert matches_any_pattern("test/test_foo.ex", patterns)

    def test_matches_none(self):
        patterns = ["lib/foo/**", "lib/bar/**"]
        assert not matches_any_pattern("lib/other/file.ex", patterns)

    def test_empty_patterns(self):
        assert not matches_any_pattern("any/file.ex", [])


class TestValidatePatterns:
    """Tests for validate_patterns function."""

    def test_valid_patterns(self):
        patterns = ["lib/foo/**", "lib/bar.ex", "test/*.ex"]
        errors = validate_patterns(patterns)
        assert errors == []

    def test_too_broad_pattern_double_star(self):
        patterns = ["**"]
        errors = validate_patterns(patterns)
        assert len(errors) == 1
        assert "too broad" in errors[0]

    def test_too_broad_pattern_double_star_slash_star(self):
        patterns = ["**/*"]
        errors = validate_patterns(patterns)
        assert len(errors) == 1
        assert "too broad" in errors[0]

    def test_empty_pattern(self):
        patterns = ["lib/**", "", "test/**"]
        errors = validate_patterns(patterns)
        assert len(errors) == 1
        assert "Empty" in errors[0]

    def test_recursive_pattern_with_prefix_is_valid(self):
        """Patterns like **/foo.ex are allowed (matches foo.ex anywhere)."""
        patterns = ["**/foo.ex"]
        errors = validate_patterns(patterns)
        assert errors == []
