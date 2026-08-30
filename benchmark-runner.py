#!/usr/bin/env python3
"""
benchmark_a.py

Code-intelligence efficiency benchmark for Claude Code.

Measures:
  - input/output/total tokens
  - token reduction vs. the "none" condition
  - MCP calls and MCP tools used
  - execution duration
  - files changed / diff size for implementation runs

Does NOT:
  - build repositories
  - run tests
  - judge implementation correctness
  - assign the 1-10 code-intelligence quality rating

The quality/correctness evaluation belongs in Tool B, where Claude context
can be deliberately preserved for the evaluation.

The benchmark is:

    TOOLS × REPOS × TASKS

Repo Overview is the same task for every repository.

Planning and Implementation use repository-specific feature_tasks.

Each run starts from a clean git worktree at the configured commit, so
conditions never see changes made by another condition.

Requirements:
  - Python 3.10+
  - git
  - Claude Code CLI (`claude`)
  - local checkouts of the benchmark repositories OR URLs in REPOS

Run:
    python3 benchmark_a.py

Useful:
    python3 benchmark_a.py --dry-run
    python3 benchmark_a.py --tool none
    python3 benchmark_a.py --repo k6
    python3 benchmark_a.py --task overview
    python3 benchmark_a.py --repo k6 --feature add-feature-x

Results:
    benchmark-results/
      runs/<repo>/<condition>/<task>[/<feature>]/
        result.json
        claude-stream.jsonl
        diff.patch
      summary.json
      summary.md
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# =============================================================================
# CONFIGURATION
# =============================================================================

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "benchmark-results"
WORKTREES_DIR = ROOT / ".benchmark-worktrees"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

INPUTS_JSON = ROOT / "inputs.json"
CONFIGS_JSON = ROOT / "configs" / "configs.json"
GLOBAL_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Optional safety/cost limits. None means no artificial benchmark limit.
MAX_TURNS: int | None = None
MAX_BUDGET_USD: float | None = None

# ---------------------------------------------------------------------------
# Load inputs.json — single source of truth for model, repos, and tools.
# MODEL can still be overridden by the CLAUDE_MODEL env var.
# ---------------------------------------------------------------------------
try:
    _inputs: dict[str, Any] = json.loads(INPUTS_JSON.read_text(encoding="utf-8"))
except FileNotFoundError:
    print(f"ERROR: {INPUTS_JSON} not found.", file=sys.stderr)
    sys.exit(1)
except json.JSONDecodeError as _exc:
    print(f"ERROR: Cannot parse {INPUTS_JSON}: {_exc}", file=sys.stderr)
    sys.exit(1)

MODEL: str = os.environ.get("CLAUDE_MODEL") or _inputs.get("model", "sonnet")
PERMISSION_MODE: str = _inputs.get("permission_mode", "acceptEdits")


# =============================================================================
# TOOL CONDITIONS AND REPOSITORIES  (loaded from inputs.json)
# =============================================================================
#
# Edit inputs.json to change the benchmark configuration.
# Claude Code uses --strict-mcp-config so each run sees only the MCP servers
# declared for that condition; MCP + hooks templates live in configs/.
#

TOOLS: list[dict[str, Any]] = _inputs.get("tools", [])
REPOS: list[dict[str, Any]] = _inputs.get("repos", [])

# Populated by validate_config() at startup. Maps engine name to its config dict.
ENGINE_CONFIGS: dict[str, dict[str, str | None]] = {}


def load_engine_configs() -> dict[str, dict[str, str | None]]:
    if not CONFIGS_JSON.exists():
        die(f"Engine config index not found: {CONFIGS_JSON}")
    try:
        data = json.loads(CONFIGS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"Cannot parse {CONFIGS_JSON}: {exc}")
    return {
        e["name"]: e.get("config", {})
        for e in data.get("engines", [])
        if e.get("name")
    }


# =============================================================================
# GLOBAL TASK
# =============================================================================

OVERVIEW_TASK = """
You are unfamiliar with this repository.

Perform an architecture review of the repository.

Identify:
- the major components, modules, packages, or services
- the responsibility of each major component
- important dependency relationships
- the major runtime and/or data flows
- important extension points
- important architectural decisions or patterns
- noteworthy architectural risks or coupling

Focus on the architecture and relationships that an engineer would need
to understand before making a non-trivial change.

Do not modify any files.
Do not build the project.
Do not run tests.

Produce a concise but useful architecture review.
"""


PLANNING_TASK_TEMPLATE = """
You need to plan the implementation of the following feature in this
repository:

--- FEATURE ---
{feature}
--- END FEATURE ---

You are unfamiliar with this repository.

Investigate the existing implementation and produce a concrete implementation
plan.

Identify:
- the relevant files
- relevant classes, functions, modules, packages, or services
- existing patterns that the change should follow
- important dependency relationships
- interfaces/APIs that need to change
- tests that should be added or modified
- important implementation risks

Do not modify any files.
Do not build the project.
Do not run tests.

The goal is a plan that another engineer could use to implement the feature
without repeating the repository investigation.
"""


IMPLEMENTATION_TASK_TEMPLATE = """
Implement the following feature in this repository:

--- FEATURE ---
{feature}
--- END FEATURE ---

Before making changes, investigate the existing implementation and follow
the repository's existing architecture, conventions, and patterns.

Requirements:
- implement the requested feature
- make the smallest reasonable set of changes
- do not refactor unrelated code
- add or update tests where appropriate
- do not build the project
- do not run tests
- do not install dependencies
- do not start external services

At the end, summarize:
- what you changed
- which files were changed
- any tests you added or modified
- any assumptions or limitations

Do not claim that tests pass because you did not run them.
"""


# =============================================================================
# HELPERS
# =============================================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expand(path: str | Path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


def slug(value: str) -> str:
    return "".join(
        c if c.isalnum() or c in "-_." else "_"
        for c in value
    )


def die(message: str) -> None:
    print(f"\nERROR: {message}", file=sys.stderr)
    sys.exit(1)


def run(
    command: list[str],
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=capture,
    )

    if check and result.returncode != 0:
        details = ""
        if capture:
            details = (
                f"\nstdout:\n{result.stdout}"
                f"\nstderr:\n{result.stderr}"
            )
        raise RuntimeError(
            f"Command failed ({result.returncode}): "
            f"{' '.join(command)}{details}"
        )

    return result


# =============================================================================
# REPOSITORY PREPARATION
# =============================================================================

def ensure_repo(repo: dict[str, Any]) -> Path:
    local_path = expand(repo["local_path"])

    if local_path.exists():
        if not (local_path / ".git").exists():
            die(f"{local_path} exists but is not a git repository.")

        return local_path

    if not repo.get("url"):
        die(
            f"Repository {repo['name']} does not exist at {local_path} "
            "and has no URL configured."
        )

    LOCAL_REPOS_DIR.mkdir(parents=True, exist_ok=True)
    destination = LOCAL_REPOS_DIR / repo["name"]

    if destination.exists():
        return destination

    print(f"  Cloning {repo['name']}...")
    run(["git", "clone", repo["url"], str(destination)])

    return destination


def repository_head(repo_path: Path) -> str:
    return run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
    ).stdout.strip()


def repository_is_clean(repo_path: Path) -> bool:
    result = run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
    )
    return not result.stdout.strip()


def resolve_commit(repo: dict[str, Any], repo_path: Path) -> str:
    configured = repo.get("commit")

    if configured:
        result = run(
            ["git", "rev-parse", configured],
            cwd=repo_path,
        )
        return result.stdout.strip()

    return repository_head(repo_path)


def create_worktree(
    repo_path: Path,
    commit: str,
    worktree: Path,
) -> None:
    if worktree.exists():
        shutil.rmtree(worktree)

    worktree.parent.mkdir(parents=True, exist_ok=True)

    run(
        [
            "git",
            "worktree",
            "add",
            "--detach",
            str(worktree),
            commit,
        ],
        cwd=repo_path,
    )


def remove_worktree(
    repo_path: Path,
    worktree: Path,
) -> None:
    run(
        [
            "git",
            "worktree",
            "remove",
            "--force",
            str(worktree),
        ],
        cwd=repo_path,
        check=False,
    )

    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)


def git_diff(worktree: Path) -> str:
    tracked = run(
        ["git", "diff", "--no-ext-diff"],
        cwd=worktree,
    ).stdout

    untracked = run(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
        ],
        cwd=worktree,
    ).stdout

    return (
        tracked
        + (
            "\n\n# UNTRACKED FILES\n"
            + untracked
            if untracked.strip()
            else ""
        )
    )


def changed_files(worktree: Path) -> list[str]:
    result = run(
        [
            "git",
            "status",
            "--porcelain",
        ],
        cwd=worktree,
    ).stdout

    files = []

    for line in result.splitlines():
        if not line:
            continue

        # Porcelain v1: XY filename
        filename = line[3:] if len(line) >= 4 else line
        files.append(filename)

    return files


# =============================================================================
# MCP CONFIGURATION
# =============================================================================

EMPTY_MCP_CONFIG = {
    "mcpServers": {}
}


def prepare_mcp_config(
    tool: dict[str, Any],
    run_dir: Path,
    repo_name: str,
) -> Path:
    """
    Write a per-run MCP config to run_dir and return its path.

    For "none" (or any engine with mcp=null): write an empty config.
    For other engines: read the template, substitute {REPO_NAME} with
    repo_name, validate as JSON, write to run_dir/mcp-config.json.
    """
    engine_cfg = ENGINE_CONFIGS.get(tool["name"], {})
    mcp_rel = engine_cfg.get("mcp")

    if mcp_rel is None:
        path = run_dir / "empty-mcp.json"
        path.write_text(json.dumps(EMPTY_MCP_CONFIG, indent=2))
        return path

    template = ROOT / mcp_rel
    if not template.exists():
        raise FileNotFoundError(
            f"MCP config template for '{tool['name']}' not found: {template}"
        )

    content = template.read_text(encoding="utf-8").replace("{REPO_NAME}", repo_name)

    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Rendered MCP config for '{tool['name']}' is invalid JSON: {exc}"
        ) from exc

    dest = run_dir / "mcp-config.json"
    dest.write_text(content, encoding="utf-8")
    return dest


# =============================================================================
# SETTINGS MANAGEMENT
# =============================================================================

_PID = os.getpid()
_GLOBAL_SETTINGS_BACKUP = Path(str(GLOBAL_SETTINGS_PATH) + f".benchmark-bak-{_PID}")
_GLOBAL_LOCAL_SETTINGS_PATH = GLOBAL_SETTINGS_PATH.parent / "settings.local.json"
_GLOBAL_LOCAL_SETTINGS_BACKUP = Path(str(_GLOBAL_LOCAL_SETTINGS_PATH) + f".benchmark-bak-{_PID}")

_EMPTY_SETTINGS = "{}\n"


def _render_template(template_path: Path, repo_name: str) -> str:
    return template_path.read_text(encoding="utf-8").replace("{REPO_NAME}", repo_name)


def _validated_json(content: str, label: str) -> str:
    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Rendered {label} is not valid JSON: {exc}") from exc
    return content


def apply_engine_config(
    tool: dict[str, Any],
    workspace: Path,
    result_dir: Path,
    repo_name: str,
) -> dict[str, Any]:
    """Apply all config files for this engine. Returns a context dict for restore."""
    engine_cfg = ENGINE_CONFIGS.get(tool["name"], {})
    ctx: dict[str, Any] = {
        "wrote_global": False,
        "had_original_global": GLOBAL_SETTINGS_PATH.exists(),
        "wrote_global_local": False,
        "had_original_global_local": _GLOBAL_LOCAL_SETTINGS_PATH.exists(),
        "mcp_config": None,
    }

    # Always neutralize ~/.claude/settings.json to prevent the user's real
    # global hooks (e.g. RepoWise) from bleeding into other engine runs.
    # Write the engine's global_settings template, or an empty object if none.
    gs_rel = engine_cfg.get("global_settings")
    if gs_rel is not None:
        gs_template = ROOT / gs_rel
        if not gs_template.exists():
            raise FileNotFoundError(
                f"global_settings template for '{tool['name']}' not found: {gs_template}"
            )
        content = _validated_json(
            _render_template(gs_template, repo_name),
            f"global_settings for '{tool['name']}'",
        )
    else:
        content = _EMPTY_SETTINGS

    if ctx["had_original_global"]:
        shutil.copy2(GLOBAL_SETTINGS_PATH, _GLOBAL_SETTINGS_BACKUP)
    GLOBAL_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_SETTINGS_PATH.write_text(content, encoding="utf-8")
    ctx["wrote_global"] = True

    # Always neutralize ~/.claude/settings.local.json to prevent the user's
    # machine-local hooks (e.g. CodeMap) from bleeding into other engine runs.
    if ctx["had_original_global_local"]:
        shutil.copy2(_GLOBAL_LOCAL_SETTINGS_PATH, _GLOBAL_LOCAL_SETTINGS_BACKUP)
    _GLOBAL_LOCAL_SETTINGS_PATH.write_text(_EMPTY_SETTINGS, encoding="utf-8")
    ctx["wrote_global_local"] = True

    try:
        # Always write worktree project and local settings — even when the engine
        # has no template — to neutralize any hooks the repo itself may have
        # (e.g. CodeMap writes hooks into .claude/settings.local.json on init).
        worktree_claude = workspace / ".claude"
        worktree_claude.mkdir(parents=True, exist_ok=True)

        ps_rel = engine_cfg.get("project_settings")
        if ps_rel is not None:
            ps_template = ROOT / ps_rel
            if ps_template.exists():
                content = _validated_json(
                    _render_template(ps_template, repo_name),
                    f"project_settings for '{tool['name']}'",
                )
            else:
                print(
                    f"  WARNING: project_settings template for '{tool['name']}' "
                    f"not found (using empty): {ps_template}",
                    file=sys.stderr,
                )
                content = _EMPTY_SETTINGS
        else:
            content = _EMPTY_SETTINGS
        (worktree_claude / "settings.json").write_text(content, encoding="utf-8")

        ls_rel = engine_cfg.get("local_settings")
        if ls_rel is not None:
            ls_template = ROOT / ls_rel
            if ls_template.exists():
                content = _validated_json(
                    _render_template(ls_template, repo_name),
                    f"local_settings for '{tool['name']}'",
                )
            else:
                print(
                    f"  WARNING: local_settings template for '{tool['name']}' "
                    f"not found (using empty): {ls_template}",
                    file=sys.stderr,
                )
                content = _EMPTY_SETTINGS
        else:
            content = _EMPTY_SETTINGS
        (worktree_claude / "settings.local.json").write_text(content, encoding="utf-8")

        ctx["mcp_config"] = prepare_mcp_config(tool, result_dir, repo_name)

    except Exception:
        _do_restore_global(ctx)
        raise

    return ctx


def restore_engine_config(ctx: dict[str, Any]) -> None:
    """Restore ~/.claude/settings.json and settings.local.json. Must not raise."""
    try:
        _do_restore_global(ctx)
    except Exception as exc:
        print(
            f"WARNING: Failed to restore global Claude settings: {exc}",
            file=sys.stderr,
        )


def _do_restore_global(ctx: dict[str, Any]) -> None:
    if ctx.get("wrote_global"):
        if ctx["had_original_global"] and _GLOBAL_SETTINGS_BACKUP.exists():
            shutil.move(str(_GLOBAL_SETTINGS_BACKUP), str(GLOBAL_SETTINGS_PATH))
        elif not ctx["had_original_global"]:
            GLOBAL_SETTINGS_PATH.unlink(missing_ok=True)

    if ctx.get("wrote_global_local"):
        if ctx["had_original_global_local"] and _GLOBAL_LOCAL_SETTINGS_BACKUP.exists():
            shutil.move(str(_GLOBAL_LOCAL_SETTINGS_BACKUP), str(_GLOBAL_LOCAL_SETTINGS_PATH))
        elif not ctx["had_original_global_local"]:
            _GLOBAL_LOCAL_SETTINGS_PATH.unlink(missing_ok=True)


# =============================================================================
# PROMPT CONSTRUCTION
# =============================================================================

def planning_prompt(feature: dict[str, Any]) -> str:
    return PLANNING_TASK_TEMPLATE.format(
        feature=feature["description"].strip()
    )


def implementation_prompt(feature: dict[str, Any]) -> str:
    return IMPLEMENTATION_TASK_TEMPLATE.format(
        feature=feature["description"].strip()
    )


def task_prompt(
    task_type: str,
    feature: dict[str, Any] | None,
) -> str:
    if task_type == "overview":
        return OVERVIEW_TASK.strip()

    if feature is None:
        raise ValueError(
            f"{task_type} requires a feature task."
        )

    if task_type == "planning":
        return planning_prompt(feature).strip()

    if task_type == "implementation":
        return implementation_prompt(feature).strip()

    raise ValueError(f"Unknown task type: {task_type}")


# =============================================================================
# CLAUDE EXECUTION + METRICS
# =============================================================================

def collect_tool_uses_from_event(
    event: Any,
    mcp_tools: dict[str, int],
) -> None:
    """
    Recursively inspect a Claude stream-json event for MCP tool_use blocks.

    Claude Code represents MCP tools using names such as:
        mcp__codemap__search

    We deliberately collect only MCP tools here. Built-in Read/Grep/Glob/etc.
    are not counted as MCP calls.
    """

    if isinstance(event, dict):
        if event.get("type") == "tool_use":
            name = event.get("name")

            if isinstance(name, str) and name.startswith("mcp__"):
                mcp_tools[name] = mcp_tools.get(name, 0) + 1

        for value in event.values():
            collect_tool_uses_from_event(value, mcp_tools)

    elif isinstance(event, list):
        for value in event:
            collect_tool_uses_from_event(value, mcp_tools)


def collect_usage_from_event(
    event: Any,
    usage_events: list[dict[str, Any]],
) -> None:
    """
    Recursively find usage objects.

    Claude Code's stream output can contain usage metadata at different
    event levels. We preserve all discovered usage objects and later prefer
    an aggregate result usage when available.
    """

    if isinstance(event, dict):
        usage = event.get("usage")

        if isinstance(usage, dict):
            usage_events.append(usage)

        for value in event.values():
            collect_usage_from_event(value, usage_events)

    elif isinstance(event, list):
        for value in event:
            collect_usage_from_event(value, usage_events)


def numeric(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def extract_usage(
    events: list[dict[str, Any]],
) -> dict[str, int | None]:
    """
    Prefer Claude Code's final aggregate usage if present.

    If it isn't present, sum the distinct usage events. The raw stream is
    always retained, so this parser can be improved later without rerunning
    the benchmark.
    """

    # First look for a final/result event containing aggregate usage.
    for event in reversed(events):
        if event.get("type") == "result":
            usage = event.get("usage")

            if isinstance(usage, dict):
                return {
                    "input_tokens": numeric(
                        usage.get("input_tokens")
                    ),
                    "output_tokens": numeric(
                        usage.get("output_tokens")
                    ),
                    "cache_creation_input_tokens": numeric(
                        usage.get("cache_creation_input_tokens")
                    ),
                    "cache_read_input_tokens": numeric(
                        usage.get("cache_read_input_tokens")
                    ),
                    "total_tokens": (
                        numeric(usage.get("input_tokens"))
                        + numeric(usage.get("output_tokens"))
                    ),
                }

    # Fall back to summing usage objects.
    usage_events: list[dict[str, Any]] = []

    for event in events:
        collect_usage_from_event(event, usage_events)

    if not usage_events:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "total_tokens": None,
        }

    return {
        "input_tokens": sum(
            numeric(x.get("input_tokens"))
            for x in usage_events
        ),
        "output_tokens": sum(
            numeric(x.get("output_tokens"))
            for x in usage_events
        ),
        "cache_creation_input_tokens": sum(
            numeric(x.get("cache_creation_input_tokens"))
            for x in usage_events
        ),
        "cache_read_input_tokens": sum(
            numeric(x.get("cache_read_input_tokens"))
            for x in usage_events
        ),
        "total_tokens": sum(
            numeric(x.get("input_tokens"))
            + numeric(x.get("output_tokens"))
            for x in usage_events
        ),
    }


def execute_claude(
    worktree: Path,
    prompt: str,
    mcp_config: Path,
) -> dict[str, Any]:
    command = [
        CLAUDE_BIN,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--model",
        MODEL,
        "--permission-mode",
        PERMISSION_MODE,
    ]

    if MAX_TURNS is not None:
        command.extend(
            ["--max-turns", str(MAX_TURNS)]
        )

    if MAX_BUDGET_USD is not None:
        command.extend(
            ["--max-budget-usd", str(MAX_BUDGET_USD)]
        )

    command.append(prompt)

    started = time.monotonic()

    process = subprocess.Popen(
        command,
        cwd=worktree,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    events: list[dict[str, Any]] = []
    raw_lines: list[str] = []

    assert process.stdout is not None

    for line in process.stdout:
        raw_lines.append(line)

        try:
            event = json.loads(line)
            events.append(event)
        except json.JSONDecodeError:
            # Preserve unexpected output. It is evidence.
            continue

    stderr = ""
    if process.stderr is not None:
        stderr = process.stderr.read()

    returncode = process.wait()

    duration = time.monotonic() - started

    mcp_tools: dict[str, int] = {}

    for event in events:
        collect_tool_uses_from_event(
            event,
            mcp_tools,
        )

    usage = extract_usage(events)

    result_event = next(
        (
            event
            for event in reversed(events)
            if event.get("type") == "result"
        ),
        None,
    )

    return {
        "command": command,
        "returncode": returncode,
        "duration_seconds": round(duration, 2),
        "usage": usage,
        "mcp": {
            "call_count": sum(mcp_tools.values()),
            "tools": mcp_tools,
        },
        "result_event": result_event,
        "stderr": stderr,
        "raw_lines": raw_lines,
    }


# =============================================================================
# SINGLE RUN
# =============================================================================

def run_one(
    repo: dict[str, Any],
    tool: dict[str, Any],
    task_type: str,
    feature: dict[str, Any] | None,
) -> dict[str, Any]:

    repo_path = ensure_repo(repo)
    commit = resolve_commit(repo, repo_path)

    feature_name = (
        feature["name"]
        if feature
        else None
    )

    parts = [
        repo["name"],
        tool["name"],
        task_type,
    ]

    if feature_name:
        parts.append(feature_name)

    run_id = "__".join(
        slug(x)
        for x in parts
    )

    # Add a UUID suffix to make accidental workspace collisions impossible.
    workspace = (
        WORKTREES_DIR
        / f"{run_id}__{uuid.uuid4().hex[:8]}"
    )

    result_dir = (
        RESULTS_DIR
        / "runs"
        / slug(repo["name"])
        / slug(tool["name"])
        / slug(task_type)
    )

    if feature_name:
        result_dir /= slug(feature_name)

    result_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print("=" * 90)
    print(
        f"{repo['name']} | "
        f"{tool['name']} | "
        f"{task_type}"
        + (
            f" | {feature_name}"
            if feature_name
            else ""
        )
    )
    print("=" * 90)
    print(f"  Commit: {commit}")

    create_worktree(
        repo_path,
        commit,
        workspace,
    )

    started_at = now_iso()

    engine_ctx: dict[str, Any] | None = None
    try:
        engine_ctx = apply_engine_config(
            tool,
            workspace,
            result_dir,
            repo["name"],
        )
        mcp_config = engine_ctx["mcp_config"]

        prompt = task_prompt(
            task_type,
            feature,
        )

        print("  Running Claude Code...")

        claude = execute_claude(
            workspace,
            prompt,
            mcp_config,
        )

        diff = git_diff(workspace)
        files = changed_files(worktree=workspace)

        result = {
            "benchmark": {
                "version": 1,
                "started_at": started_at,
                "finished_at": now_iso(),
                "run_id": run_id,
            },

            "repo": {
                "name": repo["name"],
                "language": repo["language"],
                "source_path": str(repo_path),
                "commit": commit,
            },

            "condition": {
                "name": tool["name"],
                "description": tool["description"],
            },

            "task": {
                "type": task_type,
                "feature_name": feature_name,
                "feature_description": (
                    feature["description"].strip()
                    if feature
                    else None
                ),
            },

            "claude": {
                "model": MODEL,
                "permission_mode": PERMISSION_MODE,
                "returncode": claude["returncode"],
                "duration_seconds": claude[
                    "duration_seconds"
                ],
            },

            "usage": claude["usage"],

            "mcp": claude["mcp"],

            "implementation_observation": {
                "files_changed": files,
                "file_count": len(files),
                "diff_bytes": len(diff.encode("utf-8")),
            },

            # Tool B will populate/evaluate this later.
            "code_intelligence_quality_rating": None,

            "artifacts": {
                "raw_stream": "claude-stream.jsonl",
                "diff": "diff.patch",
            },

            "errors": {
                "claude_stderr": claude["stderr"],
            },
        }

        (result_dir / "result.json").write_text(
            json.dumps(
                result,
                indent=2,
            )
        )

        (result_dir / "claude-stream.jsonl").write_text(
            "".join(claude["raw_lines"])
        )

        (result_dir / "diff.patch").write_text(
            diff
        )

        usage = claude["usage"]

        print(
            f"  Tokens: "
            f"{usage['total_tokens'] if usage['total_tokens'] is not None else 'UNKNOWN'}"
        )
        print(
            f"  MCP calls: "
            f"{claude['mcp']['call_count']}"
        )

        if claude["mcp"]["tools"]:
            for name, count in claude["mcp"]["tools"].items():
                print(f"    {name}: {count}")

        print(
            f"  Duration: "
            f"{claude['duration_seconds']}s"
        )
        print(
            f"  Files changed: "
            f"{len(files)}"
        )
        print(
            f"  Exit code: "
            f"{claude['returncode']}"
        )

        return result

    finally:
        if engine_ctx is not None:
            restore_engine_config(engine_ctx)
        remove_worktree(
            repo_path,
            workspace,
        )


# =============================================================================
# SUMMARY / TOKEN REDUCTION
# =============================================================================

def result_key(result: dict[str, Any]) -> tuple:
    return (
        result["repo"]["name"],
        result["task"]["type"],
        result["task"]["feature_name"],
        result["condition"]["name"],
    )


def baseline_key(result: dict[str, Any]) -> tuple:
    return (
        result["repo"]["name"],
        result["task"]["type"],
        result["task"]["feature_name"],
    )


def token_reduction(
    current: int | None,
    baseline: int | None,
) -> float | None:
    if current is None or baseline is None:
        return None

    if baseline == 0:
        return None

    return round(
        ((baseline - current) / baseline) * 100,
        1,
    )


def enrich_with_reductions(
    results: list[dict[str, Any]],
) -> None:
    baselines: dict[tuple, int | None] = {}

    for result in results:
        if result["condition"]["name"] != "none":
            continue

        baselines[
            baseline_key(result)
        ] = result["usage"]["total_tokens"]

    for result in results:
        baseline = baselines.get(
            baseline_key(result)
        )

        result["usage"]["token_reduction_vs_none_pct"] = (
            token_reduction(
                result["usage"]["total_tokens"],
                baseline,
            )
        )


def fmt_tokens(value: Any) -> str:
    if value is None:
        return "n/a"

    return f"{int(value):,}"


def fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"

    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def cell_text(result: dict[str, Any] | None) -> str:
    if result is None:
        return "—"

    usage = result["usage"]
    mcp = result["mcp"]

    tools = mcp["tools"]

    if tools:
        tool_text = ", ".join(
            f"{name}: {count}"
            for name, count in tools.items()
        )
    else:
        tool_text = "none"

    rating = result.get(
        "code_intelligence_quality_rating"
    )

    rating_text = (
        str(rating)
        if rating is not None
        else "— (Tool B)"
    )

    return (
        f"**{fmt_tokens(usage['total_tokens'])}** tokens<br>"
        f"Reduction: {fmt_pct(usage.get('token_reduction_vs_none_pct'))}<br>"
        f"MCP: {mcp['call_count']} calls"
        f"<br>Tools: {tool_text}"
        f"<br>Quality: {rating_text}"
    )


def write_summary(
    results: list[dict[str, Any]],
) -> None:

    enrich_with_reductions(results)

    RESULTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Save enriched machine-readable summary.
    (RESULTS_DIR / "summary.json").write_text(
        json.dumps(
            results,
            indent=2,
        )
    )

    # Index results for matrix rendering.
    index = {
        result_key(result): result
        for result in results
    }

    tools = [
        tool["name"]
        for tool in TOOLS
    ]

    repos = [
        repo["name"]
        for repo in REPOS
    ]

    markdown: list[str] = []

    markdown.append("# Claude Code Code-Intelligence Benchmark\n")
    markdown.append(
        "Tool A measures efficiency. "
        "The 1–10 code-intelligence quality rating is intentionally "
        "left for Tool B.\n"
    )

    for repo_name in repos:

        markdown.append(f"## {repo_name}\n")

        # --------------------------------------------------------------
        # Overview
        # --------------------------------------------------------------

        markdown.append("### Repo Overview\n")

        markdown.append(
            "| Condition | Result |"
        )
        markdown.append(
            "|---|---|"
        )

        for tool in tools:
            key = (
                repo_name,
                "overview",
                None,
                tool,
            )

            markdown.append(
                f"| {tool} | "
                f"{cell_text(index.get(key))} |"
            )

        markdown.append("")

        # --------------------------------------------------------------
        # Feature tasks
        # --------------------------------------------------------------

        repo = next(
            r
            for r in REPOS
            if r["name"] == repo_name
        )

        for feature in repo["feature_tasks"]:

            feature_name = feature["name"]

            markdown.append(
                f"### Feature: `{feature_name}`\n"
            )

            markdown.append(
                "| Condition | Planning | Implementation |"
            )
            markdown.append(
                "|---|---|---|"
            )

            for tool in tools:

                planning = index.get(
                    (
                        repo_name,
                        "planning",
                        feature_name,
                        tool,
                    )
                )

                implementation = index.get(
                    (
                        repo_name,
                        "implementation",
                        feature_name,
                        tool,
                    )
                )

                markdown.append(
                    f"| {tool} | "
                    f"{cell_text(planning)} | "
                    f"{cell_text(implementation)} |"
                )

            markdown.append("")

    (RESULTS_DIR / "summary.md").write_text(
        "\n".join(markdown)
    )


# =============================================================================
# DRY RUN
# =============================================================================

def print_plan(
    selected_tools: list[dict[str, Any]],
    selected_repos: list[dict[str, Any]],
    selected_tasks: list[str],
    selected_feature: str | None,
) -> None:

    rows = []

    for repo in selected_repos:
        for tool in selected_tools:
            if "overview" in selected_tasks:
                rows.append(
                    (
                        repo["name"],
                        tool["name"],
                        "overview",
                        "",
                    )
                )

            if "planning" in selected_tasks:
                for feature in repo["feature_tasks"]:
                    if (
                        selected_feature
                        and feature["name"]
                        != selected_feature
                    ):
                        continue

                    rows.append(
                        (
                            repo["name"],
                            tool["name"],
                            "planning",
                            feature["name"],
                        )
                    )

            if "implementation" in selected_tasks:
                for feature in repo["feature_tasks"]:
                    if (
                        selected_feature
                        and feature["name"]
                        != selected_feature
                    ):
                        continue

                    rows.append(
                        (
                            repo["name"],
                            tool["name"],
                            "implementation",
                            feature["name"],
                        )
                    )

    print()
    print(
        f"Planned runs: {len(rows)}"
    )
    print()

    for i, row in enumerate(rows, 1):
        repo, tool, task, feature = row

        suffix = (
            f" [{feature}]"
            if feature
            else ""
        )

        print(
            f"{i:3}. "
            f"{repo:15} "
            f"{tool:12} "
            f"{task:16}"
            f"{suffix}"
        )


# =============================================================================
# VALIDATION
# =============================================================================

def validate_config() -> None:
    global ENGINE_CONFIGS
    ENGINE_CONFIGS = load_engine_configs()

    if not shutil.which(CLAUDE_BIN):
        die(
            f"Claude Code CLI not found: {CLAUDE_BIN}"
        )

    if not shutil.which("git"):
        die("git not found on PATH.")

    tool_names = set()

    for tool in TOOLS:
        name = tool["name"]

        if name in tool_names:
            die(f"Duplicate tool: {name}")

        tool_names.add(name)

        if name == "none":
            continue

        if name not in ENGINE_CONFIGS:
            print(
                f"WARNING: Engine '{name}' has no entry in {CONFIGS_JSON}",
                file=sys.stderr,
            )
            continue

        cfg = ENGINE_CONFIGS[name]
        for key in ("mcp", "global_settings", "project_settings", "local_settings"):
            rel = cfg.get(key)
            if rel is not None and not (ROOT / rel).exists():
                print(
                    f"WARNING: {key} template for '{name}' "
                    f"not found: {ROOT / rel}",
                    file=sys.stderr,
                )

    repo_names = set()

    for repo in REPOS:
        name = repo["name"]

        if name in repo_names:
            die(f"Duplicate repository: {name}")

        repo_names.add(name)

        if not repo.get("feature_tasks"):
            die(
                f"Repository '{name}' has no feature_tasks."
            )

        for feature in repo["feature_tasks"]:
            if not feature.get("name"):
                die(
                    f"Repository '{name}' has a feature without a name."
                )

            if not feature.get("description"):
                die(
                    f"Repository '{name}' feature "
                    f"'{feature['name']}' has no description."
                )


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Run Claude Code code-intelligence efficiency benchmark."
        )
    )

    parser.add_argument(
        "--tool",
        help="Only run one condition.",
    )

    parser.add_argument(
        "--repo",
        help="Only run one repository.",
    )

    parser.add_argument(
        "--task",
        choices=[
            "overview",
            "planning",
            "implementation",
        ],
        action="append",
        help="Only run selected task type(s). Can be repeated.",
    )

    parser.add_argument(
        "--feature",
        help="Only run a named feature task.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned runs without executing Claude.",
    )

    args = parser.parse_args()

    validate_config()

    selected_tools = TOOLS

    if args.tool:
        selected_tools = [
            tool
            for tool in TOOLS
            if tool["name"] == args.tool
        ]

        if not selected_tools:
            die(
                f"Unknown tool: {args.tool}"
            )

    selected_repos = REPOS

    if args.repo:
        selected_repos = [
            repo
            for repo in REPOS
            if repo["name"] == args.repo
        ]

        if not selected_repos:
            die(
                f"Unknown repository: {args.repo}"
            )

    selected_tasks = (
        args.task
        if args.task
        else [
            "overview",
            "planning",
            "implementation",
        ]
    )

    # Validate feature selector.
    if args.feature:
        found = any(
            feature["name"] == args.feature
            for repo in selected_repos
            for feature in repo["feature_tasks"]
        )

        if not found:
            die(
                f"Feature not found: {args.feature}"
            )

    print()
    print("Claude Code Code-Intelligence Benchmark")
    print("----------------------------------------")
    print(f"Model: {MODEL}")
    print(f"Permission mode: {PERMISSION_MODE}")
    print(
        "Builds/tests: DISABLED by benchmark design"
    )

    if args.dry_run:
        print_plan(
            selected_tools,
            selected_repos,
            selected_tasks,
            args.feature,
        )
        return

    RESULTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    WORKTREES_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    all_results: list[dict[str, Any]] = []

    # Keep the outer loop as tool -> repo -> task so the benchmark can be
    # stopped/restarted by condition if desired.
    for tool in selected_tools:

        for repo in selected_repos:

            for task_type in selected_tasks:

                if task_type == "overview":

                    result = run_one(
                        repo=repo,
                        tool=tool,
                        task_type="overview",
                        feature=None,
                    )

                    all_results.append(result)

                else:

                    for feature in repo["feature_tasks"]:

                        if (
                            args.feature
                            and feature["name"]
                            != args.feature
                        ):
                            continue

                        result = run_one(
                            repo=repo,
                            tool=tool,
                            task_type=task_type,
                            feature=feature,
                        )

                        all_results.append(result)

    write_summary(all_results)

    print()
    print("=" * 90)
    print("BENCHMARK COMPLETE")
    print("=" * 90)
    print(
        f"Results: {RESULTS_DIR}"
    )
    print(
        f"Summary: {RESULTS_DIR / 'summary.md'}"
    )


if __name__ == "__main__":
    main()
