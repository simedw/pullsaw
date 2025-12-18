"""Pattern matching utilities for allowlist enforcement."""

from pathlib import PurePosixPath


def matches_pattern(filepath: str, pattern: str) -> bool:
    """Check if a filepath matches a single pattern.

    Supports:
    - Exact paths: "lib/foo.ex"
    - Directory globs: "lib/auth/**" (matches lib/auth/foo.ex, lib/auth/sub/bar.ex)
    - Single segment globs: "lib/*/helpers.ex"
    - PurePosixPath.match() for standard glob patterns
    """
    path = PurePosixPath(filepath)

    # Handle ** at end (directory and all subdirectories)
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        # Check if filepath starts with the prefix directory
        if filepath.startswith(prefix + "/") or filepath == prefix:
            return True
        # Also try PurePosixPath.match for edge cases
        return path.match(pattern)

    # Handle ** in the middle
    if "**" in pattern:
        # PurePosixPath.match handles ** reasonably well
        return path.match(pattern)

    # Exact match or simple glob
    if "*" in pattern:
        return path.match(pattern)

    # Exact path match
    return filepath == pattern


def matches_any_pattern(filepath: str, patterns: list[str]) -> bool:
    """Check if filepath matches any of the given patterns."""
    return any(matches_pattern(filepath, p) for p in patterns)


def validate_patterns(patterns: list[str]) -> list[str]:
    """Validate patterns and return any errors.

    Rules:
    - No bare "**" (must have a directory prefix)
    - No empty patterns
    """
    errors: list[str] = []

    for pattern in patterns:
        if not pattern:
            errors.append("Empty pattern not allowed")
            continue

        if pattern == "**" or pattern == "**/*":
            errors.append(f"Pattern '{pattern}' is too broad - must have a directory prefix")
            continue

        # Pattern starting with ** without directory is suspicious
        if pattern.startswith("**/") and pattern.count("/") == 1:
            # e.g., "**/foo.ex" - matches any file named foo.ex anywhere
            # This is allowed but worth noting it's broad
            pass

    return errors
