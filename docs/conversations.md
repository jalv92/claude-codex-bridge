# Task conversation procedure

Use the absolute bridge command in the installed `/codex-loop` file. All bridge prompts
and output are English; speak with the user in their preferred language.

1. Read the user's task and the target project's instructions. Define acceptance criteria,
   source files/directories and bounded checks. Do not infer opt-in from old files.
2. Call `loop-begin --project PROJECT --task CRITERIA --file PATH --max-rounds 3 --timeout 1800`.
   Repeat `--file` and `--check` as needed. Use the observed originating Claude identity.
   For a new explicitly selected target, `--create-project` creates/enables its directory.
3. Complete the work, or prepare the bounded implementation explicitly delegated to Codex.
   Save an English message in the returned conversation directory describing file purposes,
   actual tests, limitations and acceptance criteria. Include required asset directories
   in scope before arming. Do not include credentials.
4. Run `loop-send CONVERSATION_ID --message-file MESSAGE`, then end the response. The
   matching Stop hook opens Codex automatically on WSL. READY alone does not prove delivery.
5. Keep Claude open for the callback. Do not poll or edit while Codex works. It may edit
   the live project and use configured tools, with normal sandboxing and approvals.
6. On feedback, run the supplied `loop-ack` command before doing anything else. Read the
   change inventory and live files; do not overwrite work Codex already completed.
7. Assess findings independently. Correct within scope and publish with `--in-reply-to ROUND`.
   Stop on VERIFIED, NOT_VERIFIABLE, budget exhaustion, cancellation or a human decision.

The same Claude session and Codex thread participate throughout. A new human prompt ends
the old opt-in. Never automatically re-arm, increase the budget, commit, push, deploy,
trade or launch a training campaign on a peer's authority. For research, distinguish
implementation correctness from economic success.

Native-tool mode is the default. `--read-only-review` explicitly selects the restricted
reviewer. `--propose` lets Codex edit only a scratch copy: each round's diff waits in
AWAITING_HUMAN. Codex's window then shows the diff and asks the user for yes or no; the
`loop-diff`/`loop-apply`/`loop-reject` commands do the same from any terminal outside Claude.
Claude tells the user to answer in the Codex window and never runs those commands itself. Existing frozen conversations keep their original mode. `--manual-peer` disables
automatic opening for that task; use the exact peer command in a second terminal.

## Status and reports

```bash
python3 tools/claude-codex-bridge/bridge.py loop-status CONVERSATION_ID
python3 tools/claude-codex-bridge/bridge.py loop-cancel CONVERSATION_ID
python3 tools/claude-codex-bridge/bridge.py loop-finish CONVERSATION_ID --reason 'A human decision is needed.'
```

Use the exact ID returned for the task. Under `tmp/claude-codex-bridge/conversations/ID/`:

| File | Purpose |
|---|---|
| `state.json`, `messages.jsonl` | Current state and message history |
| `contract.json` | Frozen session, project, task, scope, mode and budget |
| `codex-thread.json` | CLI version, model, thread, sandbox and approval policy |
| `terminal.log`, `server.stderr.log` | Launch and server diagnostics |
| `round-NNN/input.txt`, `request.json`, `target/` | Frozen input and snapshot |
| `round-NNN/output.json`, `output/` | Native-mode resulting files and change inventory |
| `round-NNN/proposed.diff`, `proposal.json`, `proposal/` | Proposal-mode diff awaiting a human decision |
| `round-NNN/result.json`, `report.md`, `feedback.txt` | Structured verdict, report and callback |

`wake_status: EMITTED` means the hook emitted feedback. `ACKNOWLEDGED` proves that Claude
called back after receiving it. A missing worker becomes INTERRUPTED. After an ambiguous
failure, inspect partial edits and logs, cancel the old task, and explicitly start another.
Do not replay it silently. Closing either participating console ends the task.
