# PullSaw

Split large PRs into stacked PRs using Claude Code.

## Overview

`pullsaw` analyzes a feature branch, generates a plan to split it into smaller, reviewable PRs, and executes each step using Claude Code in headless mode. Each stacked PR is self-contained with passing tests.

## Prerequisites

- Python 3.12+
- [Claude Code](https://claude.ai/code) installed and authenticated
- Git repository with a feature branch to split

## Installation

```bash
# From PyPI
pip install pullsaw

# Or with uv
uv pip install pullsaw

# For local development
pip install -e .
```

## Usage

```bash
# Run from a feature branch
git checkout my-feature-branch
pullsaw

# With options
pullsaw --base main --head my-feature --strict --yes
```

### Options

| Option | Description |
|--------|-------------|
| `--base BRANCH` | Base branch (default: auto-detect main/master) |
| `--head BRANCH` | Head branch (default: current branch) |
| `--test-cmd CMD` | Override test command (e.g., `mix test test/specific`) |
| `--check-cmd CMD` | Check command before tests (e.g., `mix compile --warnings-as-errors`) |
| `--strict` | Fail if drift is detected vs original |
| `--yes, -y` | Skip confirmation prompt |
| `--dry-run` | Generate plan only, don't execute |
| `--verbose, -v` | Show detailed output |
| `--plan FILE` | Use existing plan file instead of generating |
| `--continue` | Continue from current step branch |
| `--skip` | Skip the current failed step (use with `--continue`) |

## How It Works

1. **Check State**: Ensures clean working tree and valid branches
2. **Analyze Diff**: Gets changed files between base and head
3. **Generate Plan**: Uses Claude Code to propose a stacked PR plan
4. **Validate Plan**: Ensures all files are covered and steps are valid
5. **Execute Steps**: For each step:
   - Create a new branch
   - Claude Code implements the step
   - Enforce allowlist (rollback unauthorized changes)
   - Run format and tests
   - Fix failures (up to N attempts)
   - Commit
6. **Drift Check**: Verify final state matches original branch

### Commit Messages & revup Integration

PullSaw generates commit messages that are compatible with [revup](https://github.com/Skydio/revup), using `Topic:` and `Relative:` fields in the commit message. This allows you to upload the stacked PRs to GitHub using:

```bash
revup upload
```

## Configuration

Create `.pullsaw/config.yml` in your repo:

```yaml
test_cmd: ["mix", "test", "--max-failures", "1"]
format_cmd: ["mix", "format"]
max_fix_attempts: 5
strict: false
```

### Auto-detection

Without a config file, PullSaw auto-detects based on project files:

| File | Test Command | Format Command |
|------|--------------|----------------|
| `mix.exs` | `mix test` | `mix format` |
| `package.json` | `npm/yarn/pnpm test` | `npm run format` |
| `Cargo.toml` | `cargo test` | `cargo fmt` |
| `pyproject.toml` | `pytest` | `ruff format .` |
| `go.mod` | `go test ./...` | `go fmt ./...` |

## Example Session

```
$ pullsaw

╭──────────────────────────────────╮
│ PullSaw - Stacked PR Splitter      │
╰──────────────────────────────────╯

Checking working tree... clean
Base: main
Head: feature-auth
Changed files: 12

Generating plan via Claude Code...

         Stacked PR Plan
┏━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ # ┃ Title                         ┃ Files             ┃
┡━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ 1 │ Introduce auth types          │ lib/auth/**       │
│ 2 │ Add auth plug + config        │ lib/auth/plug.ex  │
│ 3 │ Wire up routes                │ lib/web/router.ex │
└───┴───────────────────────────────┴───────────────────┘

Proceed with execution? [y/n] y

Executing plan...

[1/3] Introduce auth types
  Created branch: feature-auth-step-1
  Claude Code: implementing...
  Format: OK
  Tests: PASS
  Committed: step(1): Introduce auth types

[2/3] Add auth plug + config
  Created branch: feature-auth-step-2
  Claude Code: implementing...
  Format: OK
  Tests: PASS
  Committed: step(2): Add auth plug + config

[3/3] Wire up routes
  Created branch: feature-auth-step-3
  Claude Code: implementing...
  Format: OK
  Tests: PASS
  Committed: step(3): Wire up routes

No drift - stack matches original!

Done! Created branches:
  - feature-auth-step-1
  - feature-auth-step-2
  - feature-auth-step-3
```

## Architecture

- **Python CLI** (this tool): Orchestration, validation, git operations, test execution
- **Claude Code**: LLM work - planning, editing files, fixing failures

Python owns the source of truth for:
- Branch creation and naming
- Allowlist enforcement
- Running tests and format commands
- Committing changes

Claude Code never has permission to commit, push, or checkout branches.

## Author

Created by [Simon Edwardsson](https://simedw.com)

## License

MIT

