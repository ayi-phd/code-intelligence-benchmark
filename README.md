# Code Intelligence Benchmark

A lightweight benchmark for measuring how code-intelligence tools affect Claude Code's token usage and effectiveness when working with unfamiliar repositories.

The benchmark is designed to compare code-intelligence engines under the same repository, task, and model conditions.

## Benchmark Conditions

The benchmark compares:

* **None** — Claude Code without code intelligence
* **RepoWise**
* **Graphify**
* **CodeMap**
* **CodeIntel** — added later when CodeIntel is ready

## Benchmark Tasks

Each repository is evaluated with three task types:

1. **Repository Overview**
   Claude Code receives the same architecture-review task for every repository.

2. **Implementation Planning**
   Claude Code creates an implementation plan for a repository-specific feature.

3. **Implementation**
   Claude Code implements the same feature used in the planning task.

Feature requirements are defined per repository in `inputs.json`, because the appropriate feature task depends on the repository's domain and existing functionality.

## Metrics

For each benchmark cell, the runner records:

* Input tokens
* Output tokens
* Total tokens
* Token reduction versus the `None` baseline
* MCP tools called
* MCP tool-call information needed for subsequent evaluation

The benchmark is intentionally split into two tools:

* **Benchmark A** measures token usage and records MCP activity.
* **Benchmark B** will separately evaluate the correctness/usefulness of code-intelligence output while preserving the relevant Claude Code context.

## Repositories

The repositories being analyzed are **not part of this repository**. They are maintained as separate local Git clones.

The current benchmark subjects are:

* Miniflux — Go
* Halo — Java
* Formbricks — TypeScript
* pretix — Python

Their local paths and repository-specific feature tasks are configured in `inputs.json`.

For reproducibility, the runner records the current `HEAD` commit for each repository with:

```bash
git rev-parse HEAD
```

The benchmark does not fetch, checkout, or modify the benchmark repositories.

## Configuration

All benchmark inputs are externalized in:

```text
inputs.json
```

This includes:

* benchmark repositories
* local repository paths
* repository-specific feature requirements
* code-intelligence engines
* Claude Code configuration

The goal is to keep `benchmark-runner.py` focused on executing the experiment rather than containing experiment-specific data.

## Running

From the benchmark repository:

```bash
./benchmark-runner.py
```

or:

```bash
python3 benchmark-runner.py
```

Results are written to:

```text
results/
```

## Design Principles

### Same task, different intelligence

Within a repository, the task is held constant while the code-intelligence condition changes.

### Same repository state

The Git commit used for each repository is recorded with the benchmark results.

### Use the real tool integration

Each code-intelligence engine should be evaluated using its intended Claude Code integration, including MCP servers and hooks where applicable. The benchmark should not artificially instruct Claude Code to use an engine in a way that differs from its normal integration.

### Keep it simple

This is an experimental benchmark, not a benchmarking framework.

The initial goal is to obtain useful comparative measurements quickly. Complexity should only be added when it is necessary to answer a specific benchmarking question.

