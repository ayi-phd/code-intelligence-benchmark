# Code Intelligence Benchmark

A lightweight benchmark for measuring how code-intelligence tools affect Claude Code's token usage and effectiveness across real-world repositories and development tasks.

## Git Flow

- `master` is the production branch.
- `develop` is the default working branch.
- All feature branches start from `develop`.

For each new task or feature:

1. Ask whether I want a new feature branch.
2. If yes, find the greatest `NNN` among existing `feat/NNN-*` branches and create `feat/NNN-brief-feature-name` from `develop`, incrementing `NNN` by 1.
3. Implement and test the task on that branch.
4. When complete, stage changes and draft a commit message beginning with the task number, e.g. `Feature 003 Implement Tree-sitter predicates`. **Ask for approval before committing.**
5. After commit approval, commit the changes. **Ask for approval before pushing.**
6. After push, prompt: **"Please create a PR `feat/NNN-...` → `develop` in GitHub, review it, and let me know when it's merged."**
7. After merge is explicitly confirmed:
   ```bash
   git switch develop
   git pull origin develop
   ```

**Never commit, push, or switch branches without explicit approval at that step.**