# Claude–Codex Bridge

Python 3.11+ standard-library tooling for explicitly opted-in local agent conversations.
Read README.md, docs/installation.md and docs/architecture.md before changing behavior.

- Source lives in tools/claude-codex-bridge/. Keep its location: runtime defaults and
  subprocess entrypoints are resolved relative to the repository root.
- Keep source, schemas, tests and templates versioned. tmp/ contains private runtime data.
- Never publish credentials, local configuration, transcripts or generated job reports.
- Preserve native CLI sandboxing and approval behavior. Never auto-approve tool requests.
- Preserve task/session/project binding, immutable inputs and explicit opt-in.
- Never start another model or Claude session as an ordinary test. Real smoke tests need
  explicit authorization; Claude SessionStart hooks can have synchronization side effects.
- Run python3 -m unittest discover -s tools/claude-codex-bridge/tests -v and
  python3 -m unittest discover -s tests -v after changes to orchestration or installation.
- Use English for code, documentation, CLI output and agent prompts.
- Do not commit or push unless the user's task authorizes publishing changes.
