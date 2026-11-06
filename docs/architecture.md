# Architecture and boundaries

There are two independent workflows:

| Workflow | Codex interface | Target | Result |
|---|---|---|---|
| Saved review | `codex exec` | Frozen committed Git code plus declared artifacts | Saved report; no automatic correction |
| Task conversation | Task-local app-server and native TUI | Scoped working files, including uncommitted/new files | Automatic Claude callback; bounded review/correction rounds |

Python standard-library modules separate input contracts, filesystem/process state,
conversation coordination, WebSocket transport and Windows Terminal launching. No persistent
daemon or background model watches for tasks. A Stop event without a matching ready task
does nothing. A response ending is not treated as task completion by itself.

## Identity, publication and lifecycle

The UserPromptSubmit observer records the originating Claude process/session. Arming freezes
session, canonical project, task, scope and limits. Publication hashes the message and
selected files before atomically marking the round READY. Only a matching Stop event can
claim it. File locks prevent duplicate delivery, repeated window launches and competing
native peers for the same project. Recursive events without new READY input are no-ops.

States include ARMED, READY, QUEUED, REVIEWING, REPLY_READY, AWAITING_CLAUDE, COMPLETED,
BLOCKED, FAILED, INTERRUPTED, TIMED_OUT, CANCELLED and STOPPED. Only a valid structured
VERIFIED result completes a conversation. Process exit zero is not a verdict.

## Native tools and approvals

The native mode retains configured CLI tools, apps, MCP servers, skills and model settings.
It requests workspace-write sandboxing in the selected project. User/system configuration
and managed approval policy remain effective. Hooks and notifications are disabled to
prevent recursive dispatch; credentials are not copied between platforms.

The app-server broadcasts approval and input requests to subscribed clients. The bridge
leaves them unanswered so the attached native TUI can handle them; it never sends automatic
permission grants. Pending requests are replayed when the TUI resumes the thread.
See the [official app-server documentation](https://learn.chatgpt.com/docs/app-server).

Task scope is an agent instruction, not a syscall allowlist. The frozen input remains intact;
native edits are recorded separately. Source drift before execution or after the recorded
result blocks stale feedback. Other editors are not locked: their concurrent changes cannot
be reliably distinguished from Codex's work. Never edit alongside the active peer.

## Process and terminal lifecycle

Workers are detached from hook input/output, with actual PIDs, process identities and exit
codes recorded. Deadlines terminate only the owned process tree. After ambiguous failure,
explicit recovery is required. Work does not survive machine restart or WSL shutdown.

Windows Terminal receives an argument array. Linux arguments travel as base64-encoded JSON
through a fixed bootstrap, keeping spaces and shell metacharacters as data. Prompts never
become shell code. The worker acknowledges from the new window; launcher exit zero alone
does not prove connection. See [Windows Terminal arguments](https://learn.microsoft.com/en-us/windows/terminal/command-line-arguments).

## Saved reviews

The saved-review workflow validates versioned manifests and freezes prompts and artifact
metadata. It requires a committed target and never commits, stashes or resets for the user.
Snapshots exclude ignored datasets. Declared small artifacts can be copied; large artifacts
can be referenced and checked for integrity without duplication. Missing or changed inputs
are limitations, not successful verification. See [saved reviews](saved-reviews.md).

Neither workflow automates a desktop chat or sends messages to unrelated sessions.
