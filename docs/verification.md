# Verification

## Public checkout

The bridge's 39 offline regression tests and 5 installer tests exercise:

- Non-interactive paths, spaces and shell metacharacters.
- Explicit opt-in, session/project matching, immutable inputs and stale feedback.
- Repeated/concurrent Stop events and single-window/process ownership.
- Native edits, post-turn inventories, approvals delegated to the TUI and legacy read-only mode.
- Hook/command installation and removal without overwriting unrelated configuration.
- Structured verdicts, malformed responses, failures, timeout and interrupted-worker recovery.

Run both suites from CONTRIBUTING.md. CI runs them on Linux with Python 3.11 and 3.12.
No credentials or model calls are required. Test logs are local runtime data and are not
published as part of the repository.

## Original real-CLI verification

The extracted implementation was exercised on WSL Ubuntu with Claude Code 2.1.319 and
native Codex CLI 0.167.0:

| Scenario | Observed result |
|---|---|
| Automatic window | Actual Stop hook opened Windows Terminal, WSL and native Codex |
| Bidirectional calculator test | NEEDS_FIXES → Claude corrected → VERIFIED → acknowledgement |
| Native-tool edit | Codex edited calculator.py and ran three passing assertions |
| Approval routing | Native TUI displayed a real permission request; the bridge did not answer it |
| Literal paths | A real terminal probe handled spaces and shell metacharacters as data |

These real tests used harmless fixtures and deadlines. Claude smoke tests suppressed
unrelated SessionStart hooks. Runtime transcripts, account details and fixture reports
are deliberately not exported. The author's screenshots are examples of the actual workflow,
not proof that every integration or model is compatible.

The public packaging changes are checked offline and with installer diagnostics. They do
not require another model audit. Image generation, browser and external integrations have
not all been individually exercised by this test suite.
