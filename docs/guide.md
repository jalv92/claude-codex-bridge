# Guide: modes, commands and examples

This guide explains every way to run a task through the bridge, what each option is for,
and how to use it, with example prompts you can copy. Installation is in the
[README](../README.md#quick-start) and [installation guide](installation.md).

- [What's new](#whats-new)
- [Pick a mode](#pick-a-mode)
- [Example 1: let Codex fix what it finds](#example-1-let-codex-fix-what-it-finds)
- [Example 2: read every diff before it lands](#example-2-read-every-diff-before-it-lands)
- [Example 3: reject a proposal and tell Claude why](#example-3-reject-a-proposal-and-tell-claude-why)
- [Example 4: the project changed while the proposal waited](#example-4-the-project-changed-while-the-proposal-waited)
- [Example 5: review comments only, no edits](#example-5-review-comments-only-no-edits)
- [Example 6: decide from another terminal](#example-6-decide-from-another-terminal)
- [Check what happened afterwards](#check-what-happened-afterwards)
- [What ends a task](#what-ends-a-task)
- [Command reference](#command-reference)

## What's new

Reviewing and editing are now separate permissions. You can let Codex change your
project directly, or have it only propose a diff that you read and approve.

| Feature | What it does | Why it is useful |
|---|---|---|
| **Proposal mode** (`--propose`) | Codex edits a scratch copy of the scoped files. Nothing reaches your project until you approve the diff. | You see every change before it lands. |
| **Decide in the Codex window** | When Codex finishes, its window shows the diff and asks `yes` or `no`. On `no` you can add a reason for Claude. | No third terminal and no long command. |
| **`loop-diff`, `loop-apply`, `loop-reject`** | The same decision from any terminal outside Claude while the proposal waits. | If you prefer a terminal. Keep the Codex window open: closing it ends the task. |
| **Self-approval guard** | Only a person outside Claude decides. The window takes a typed `yes` or `no` and discards anything typed before the question. The commands refuse to run under Claude or without an interactive terminal, and ask you to confirm. | Claude or a stray keystroke cannot approve Codex's work. |
| **Stale-project guard** | Apply refuses if a scoped file changed after Codex proposed, or while a native-mode conversation is editing the same project. | A diff is never written over edits it did not see. |
| **Decision record** | Every decision is logged with `decision` (`APPLIED`, `REJECTED`, `NOT_APPLIED`, `PARTIAL`) and `via` (`codex-window` or `command`). | You can always tell later what was decided and where. |

Fixes in the same release:

- A handoff no longer stalls in `READY` when Claude arms a task from one folder and ends its
  turn back in the folder it was launched from.
- A refused apply keeps its specific reason ("Project changed after the proposal was
  made") instead of a later generic one, and is written to the message history.

## Pick a mode

| You want | Mode | Codex writes to | Who applies changes | Ask Claude for |
|---|---|---|---|---|
| Codex to fix what it finds | Native (default) | The live project, inside its sandbox | Codex, with normal CLI approvals | `/codex-loop ...` |
| To read every diff first | Proposal | A scratch copy only | **You**, in the Codex window | `/codex-loop ... Use proposal mode (--propose).` |
| Comments only, no edits | Read-only review | Nothing | Claude, after weighing the findings | `/codex-loop ... Use read-only review (--read-only-review).` |
| A one-off audit of a commit | [Saved review](saved-reviews.md) | Nothing | Nobody: it writes a report | See the saved-review guide |

Rules shared by every conversation mode:

- `/codex-loop` applies to **one** task. A normal prompt never starts a conversation.
- Default budget: 3 rounds and 30 minutes. The task stops on `VERIFIED` or when the budget
  runs out, and never re-arms itself.
- Each task has a **conversation ID**. Ask Claude for it in your prompt: every command uses it.
- While Codex works or a proposal waits, **do not type a new prompt into Claude**. A new
  prompt ends the task.

## Example 1: let Codex fix what it finds

Use this when you trust Codex to edit and want the fastest loop. Codex edits the live
project with its normal sandbox and approvals; any approval prompt appears in its window.

```text
/codex-loop The date parser in src/dates.py fails on ISO strings with a timezone offset.
Fix it and have Codex review and improve the fix. Scope: --file src/dates.py
--file tests/test_dates.py. Allowed check: python3 -m pytest tests/test_dates.py.
Stop when verified, within three rounds. Give me the conversation ID.
```

What happens:

1. Claude does its part, publishes a handoff and ends its response.
2. A Codex window opens by itself (WSL and Windows Terminal) and works on the task.
3. Codex's result returns to the same Claude session, which continues within scope.
4. They take turns until `VERIFIED` or the budget ends. Claude then summarizes the outcome.

## Example 2: read every diff before it lands

Use this when the change matters enough that you want to see it first, or when you do not
want Codex editing your files directly. In proposal mode Codex has no network, no MCP
servers or apps, and its shell can write only to the scratch copy and temp directories.

```text
/codex-loop Fix subtract() in calc.py so it returns a - b. Use proposal mode (--propose).
Scope: --file calc.py --file check.sh. Allowed check: ./check.sh.
Acceptance: the "subtract" line prints PASS. Give me the conversation ID.
```

When Codex finishes, its window closes the chat and shows the diff with a question. This is
the real output from a test run:

```text
--- a/calc.py
+++ b/calc.py
@@ -20,5 +20,4 @@
 
 
 def subtract(a, b):
-    # BUG (reject test): should return a - b.
-    return b - a
+    return a - b

Codex proposes the change above. Nothing is applied yet.
Type yes to apply it to the project, or no to reject it: yes
Applied to the project. Claude continues on its own.
```

- Answer **in the Codex window**, not in Claude. Claude is not woken until you answer.
- Any answer other than `yes` or `no` asks again: `Type yes or no:`.
- After `yes`, the bridge writes the files whole and keeps their permissions. Claude wakes
  up and is told you applied the diff.

## Example 3: reject a proposal and tell Claude why

Answer `no` in the Codex window. The bridge then asks for an optional reason, which goes
to Claude with the result:

```text
Type yes to apply it to the project, or no to reject it: no
Reason for Claude (optional, Enter to skip): Too broad, only change subtract()
Rejected. Nothing changed. Claude continues on its own.
```

Your project stays exactly as it was. Claude receives your reason and treats Codex's
findings as advice only; it must not apply the rejected diff itself. If rounds remain,
Claude may prepare another round. To control what happens next, say it in the first prompt:

```text
... If I reject Codex's proposal, do not implement the change yourself: close the
conversation with loop-finish and tell me what Codex proposed and the reason I gave.
```

## Example 4: the project changed while the proposal waited

If you, an editor or another tool changes a scoped file after Codex proposed, applying
would overwrite work the diff never saw. The bridge refuses instead:

```text
Type yes to apply it to the project, or no to reject it: yes
The decision did not complete: Project changed after the proposal was made; nothing was applied
```

Nothing is written, the conversation becomes `BLOCKED` with decision `NOT_APPLIED`, and
Claude stops without applying anything. Check your edits, then start a new `/codex-loop`
task from the current files.

A different refusal happens while a native-mode conversation is editing the same project.
In that case the proposal keeps waiting: answer again after that conversation finishes.

## Example 5: review comments only, no edits

Use this when you want a second opinion and will decide what to change yourself.

```text
/codex-loop Review the session handling in src/auth/ for security issues. Use read-only
review (--read-only-review). Scope: --file src/auth/session.py --file src/auth/tokens.py.
Report findings; do not edit.
```

Codex reads the snapshot and returns findings. Claude weighs them and makes any
corrections within scope; Codex itself writes nothing.

## Example 6: decide from another terminal

If you prefer a terminal to the Codex window, open one **outside Claude** while the proposal
waits. Keep the Codex window open: closing it ends the task, and then there is nothing left
to decide. Use the exact commands Claude printed for your task. They look like this:

```bash
python3 tools/claude-codex-bridge/bridge.py loop-diff CONVERSATION_ID
python3 tools/claude-codex-bridge/bridge.py loop-apply CONVERSATION_ID
python3 tools/claude-codex-bridge/bridge.py loop-reject CONVERSATION_ID --reason 'Too broad'
```

`loop-diff` only prints the diff. `loop-apply` and `loop-reject` print it and ask you to
type `yes` to confirm. Run from Claude or without an interactive terminal, they refuse:

```text
Error: Decide in an interactive terminal outside Claude: the bridge asks you to confirm
```

These checks stop an agent from approving its own peer's work by accident. They are not a
hard security boundary against a program running as your user.

## Check what happened afterwards

```bash
python3 tools/claude-codex-bridge/bridge.py loop-status CONVERSATION_ID
```

Everything about a task is in the runtime directory, by default
`tmp/claude-codex-bridge/conversations/CONVERSATION_ID/`. If you installed with a custom
`--runtime`, the commands Claude prints include it. For proposal mode, the useful files are:

| File | Shows |
|---|---|
| `state.json` | Final `state`, plus `human_decision`, `human_reason` and `human_via` |
| `messages.jsonl` | Every message in order, including your decision |
| `round-NNN/proposed.diff` | The exact diff you were asked about |
| `codex-thread.json` | Codex's sandbox, network and approval settings for the task |

A decision line in `messages.jsonl` looks like this:

```json
{"at": "2026-11-08T15:40:26.938204+00:00", "sender": "user", "text": "Proposal applied", "round": 1, "decision": "APPLIED", "via": "codex-window"}
```

| `decision` | Meaning | Your project |
|---|---|---|
| `APPLIED` | You answered `yes` and the diff was written | Changed as proposed |
| `REJECTED` | You answered `no` | Unchanged |
| `NOT_APPLIED` | You answered `yes`, but the project had changed | Unchanged by the bridge |
| `PARTIAL` | Writing failed part-way, for example a full disk | Inspect it; the originals are in `round-NNN/target/` |

## What ends a task

Only your `yes` ever applies a proposal. A task ends in one of these ways:

| Event | Final state |
|---|---|
| Codex returns `VERIFIED` and nothing else is needed | `COMPLETED` |
| You type a new prompt into Claude | `CANCELLED` |
| You close the Codex window or press Ctrl+C in it | `INTERRUPTED` |
| The deadline passes. Time spent waiting for your answer counts. | `TIMED_OUT` |
| The round limit is reached | `LIMIT_REACHED` |
| Codex cannot verify the task (`NOT_VERIFIABLE`), an apply was refused, or the project changed during Codex's turn | `BLOCKED` |
| The Codex window could not open, or the Codex process failed | `FAILED` |
| You run `loop-cancel` | `CANCELLED` |
| Claude runs `loop-finish` | `STOPPED` |

In Windows Terminal, Ctrl+C without selected text interrupts instead of copying. If the
Codex window takes focus while you copy something, the shortcut ends the task. Start a new
`/codex-loop` task to try again: an ended task is never resumed or replayed.

## Command reference

Claude runs `loop-begin`, `loop-send` and `loop-ack` itself. You normally use only the
status and decision commands.

| Command | Who runs it | Purpose |
|---|---|---|
| `doctor` | You | Check the installation |
| `configure-terminal` | You, once | Set up the automatic Codex window (WSL and Windows Terminal) |
| `loop-begin --project P --task T --file F` | Claude | Arm one task. Options below |
| `loop-send ID --message-file M` | Claude | Publish a handoff to Codex |
| `loop-ack ID --round N` | Claude | Confirm Codex's result arrived |
| `loop-status ID` | You | Show the current state |
| `loop-diff ID` | You | Print a pending proposal |
| `loop-apply ID` / `loop-reject ID [--reason R]` | You, outside Claude | Decide a pending proposal |
| `loop-finish ID --reason R` | Claude | Close the task with a reason |
| `loop-cancel ID` | You | Cancel the task |

`loop-begin` options:

| Option | Effect |
|---|---|
| `--file PATH` | A file or directory in scope. Repeat for each one |
| `--check CMD` | A command Codex may run to verify. Repeat as needed |
| `--propose` | Proposal mode: Codex edits a scratch copy; you apply or reject. Not combinable with `--read-only-review` |
| `--read-only-review` | Read-only review: Codex comments and edits nothing |
| `--max-rounds N` | Round limit, 1 to 10 (default 3) |
| `--timeout SECONDS` | Deadline for the whole task, 30 to 7200 (default 1800) |
| `--manual-peer` | Do not open the Codex window automatically; run the printed peer command yourself |
| `--create-project` | Create and enable a project folder that is not enabled yet. Git is not required |

You do not need to remember these: describe the mode in your `/codex-loop` prompt and
Claude passes the right options.
