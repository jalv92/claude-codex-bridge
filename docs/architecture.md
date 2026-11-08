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

States include ARMED, READY, QUEUED, REVIEWING, AWAITING_HUMAN, REPLY_READY, AWAITING_CLAUDE, COMPLETED,
BLOCKED, FAILED, INTERRUPTED, TIMED_OUT, CANCELLED and STOPPED. Only a valid structured
VERIFIED result completes a conversation. Process exit zero is not a verdict.

## Proposal mode

`--propose` moves Codex's working directory to a per-conversation scratch copy, reset from
the frozen snapshot each round (file modes are kept). Thread and turn both pin
`workspaceWrite` with no extra writable roots, no network and approval policy `never`, and
`sandbox_workspace_write` is overridden so user configuration cannot widen it. The bridge
fails closed if the server reports anything else. The shell can still write the system temp
directories. MCP servers, apps, plugins and other integrations that run outside the shell
sandbox are switched off, as in read-only review.

After the turn the scratch copy is inventoried against the scope. Files from the round
snapshot are always kept, so a missing one is a real deletion. New files the live project
ignores, caches such as `__pycache__`, links and credential paths are discarded and listed
in `proposal.json`. The proposal is frozen under `round-NNN/proposal/` with `proposed.diff`
and hashed. The round enters AWAITING_HUMAN.

`loop-apply` and `loop-reject` print the diff and require an interactive terminal and a typed
`yes`. They refuse to run under a Claude process, and review children are already barred from
bridge mutations. This guards against accidental self-approval; like the other locks, it is
not a boundary against a hostile process running as the same user. Apply takes the
per-project lock that native peers hold and re-checks the live inventory against the round's
request. It replaces each file whole (keeping its mode), verifies the result and records
`output.json`, so delivery and acknowledgement see the applied files. Originals stay in
`round-NNN/target/`. Rejecting a VERIFIED round returns it to Claude instead of completing.

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
