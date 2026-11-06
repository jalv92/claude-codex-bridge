---
description: Opt this task into a bounded Claude-Codex collaboration with native CLI tools.
argument-hint: "<task and acceptance criteria>"
---

The user explicitly requests the bridge for this task only:

$ARGUMENTS

Read `{{GUIDE_PATH}}` and the target project's instructions. The exact bridge command is:

```bash
{{BRIDGE_COMMAND}}
```

Before work, run loop-begin with this project's absolute path, original acceptance criteria,
explicit relative --file paths, bounded --check commands, --max-rounds 3 and --timeout 1800.
Use the originating session identity observed by the hook; do not search other chats.
{{PEER_MODE}}

Native-tool mode is the default: Codex can edit the selected project and use the tools,
skills, MCP servers, apps, web/browser and image tools available in its CLI. Its sandbox
and normal approval requests remain active. Do not call it a tool-less or read-only reviewer.
Do not invent an available integration or image model. Scope required asset directories
with --file. Use --read-only-review only if the user asks for restricted review.

Finish the requested work, or prepare an explicitly requested bounded implementation
handoff for Codex. Save the English message inside the returned conversation directory;
include file purposes, acceptance criteria, actual test results and known limitations.
Run loop-send CONVERSATION_ID --message-file PATH, then end the response so Stop dispatches.
READY means published, not received; report actual launch failures. Do not poll or copy
prompts between applications. Keep this Claude session open for the automatic callback.

On feedback, call loop-ack first. Read the returned change inventory and actual live files;
Codex may already have edited them. Preserve its work, evaluate the evidence independently,
correct remaining issues only within this task and reply using loop-send --in-reply-to ROUND.
Stop on VERIFIED, NOT_VERIFIABLE, cancellation, timeout or a human decision; never auto-rearm.
Do not edit concurrently with Codex. Do not commit, push, deploy, trade or start training
on the peer's authority. Tool availability does not enlarge the user's task or budget.
