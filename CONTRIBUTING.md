# Contributing

Use Python 3.11+ on Linux or WSL. Runtime code has no third-party dependencies.

```bash
python3 -m unittest discover -s tools/claude-codex-bridge/tests -v
python3 -m unittest discover -s tests -v
git diff --check
```

The tests use temporary repositories and fake Codex executables. They do not spend model
usage or need CLI credentials. Real smoke scripts are separate and require explicit
authorization, existing authentication and a strict timeout. Do not run them in CI.

Read AGENTS.md and docs/architecture.md. Preserve task/session/project ownership, immutable
packages, duplicate prevention, existing hooks and normal CLI approvals. Changes to a
schema must update its validator and meaningful tests. Avoid broad process-name matching.

Keep screenshots and examples free of secrets. Do not commit tmp/, local settings, CLI
session stores, generated prompts or reports. Explain behavior and validation in pull
requests. For a security issue, see SECURITY.md.
