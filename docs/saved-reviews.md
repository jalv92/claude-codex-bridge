# Independent saved reviews

This mode ends with a saved report. It does not start the conversation/correction loop.
The target must have its own Git repository and a committed final review target.

```bash
python3 tools/claude-codex-bridge/bridge.py configure --project /absolute/project
```

Inside the opted-in Claude task, use the installed absolute bridge path:

```bash
python3 /absolute/bridge/tools/claude-codex-bridge/bridge.py arm \
  --project /absolute/project --task 'Independently verify this task'
```

Save the returned job ID and draft paths. Complete `draft/prompt.md` using all six template
sections, and `draft/request.json` using the generated request/schema. Include final commit,
branch, bounded review scope, timeout and relevant artifact integrity metadata. Actual test
results belong in the prompt; expected results do not count as tests performed.

```bash
python3 /absolute/bridge/tools/claude-codex-bridge/bridge.py submit JOB_ID
```

Submission validates and atomically freezes the package. The originating session's matching
Stop hook claims it and launches a short-lived worker. Codex exec inspects the specified
committed snapshots with read-only permissions, or explicitly scoped scratch writes.
It returns VERIFIED, NEEDS_FIXES or NOT_VERIFIABLE separately from execution status.

```bash
python3 tools/claude-codex-bridge/bridge.py status JOB_ID
python3 tools/claude-codex-bridge/bridge.py run JOB_ID --recover --wait
```

Recovery is explicit after interruption; inspect saved status/logs first. Reports and
structured results are under the job's attempt directory in the ignored runtime.
Do not auto-commit, discard work, run broad experiments or enlarge the user's scope to
make a package pass validation. Large ignored artifacts must be declared explicitly.
