# Installation

Run setup from a Git clone of this repository. Python 3.11+ is required (`tomllib` is used).
There are no pip dependencies. Runtime orchestration targets Linux; WSL is the tested
environment for automatic Windows Terminal windows. Use native Linux CLI binaries, not
Windows Codex or Claude wrappers. Existing CLI authentication is reused.

## Install for one project

```bash
python3 scripts/setup.py --project /absolute/path/to/project
```

Setup resolves this checkout's entrypoint and Python interpreter, probes installed CLI
capabilities, enables the explicit target, installs its hooks in
`.claude/settings.local.json`, and renders `.claude/commands/codex-loop.md` with absolute
paths. A target does not need a Git repository for conversation mode. Neither setup nor
hook installation arms a task or starts an audit.

Both installed files contain machine-local settings; exclude them from your target's
version control. The reusable command template is versioned in this repository.
Unrelated settings and hooks are preserved. An existing unrelated `/codex-loop` file
causes setup to stop rather than overwrite it. Repeated installation is idempotent.

The CLI discovers common native Bun/npm/nvm installations. If discovery cannot resolve
your installation, provide an absolute executable. Windows Terminal may also need an
explicit location when the non-interactive PATH lacks it:

```bash
python3 scripts/setup.py --project /absolute/path/to/project \
  --codex /absolute/path/to/native/codex \
  --wt /mnt/c/Users/YOUR_WINDOWS_USER/AppData/Local/Microsoft/WindowsApps/wt.exe \
  --distribution Ubuntu
```

No interactive shell or nvm initialization is needed when the hooks run. Do not move the
checkout after installation: installed hooks contain absolute paths. Remove the old
installation before relocating it. Setup does not upgrade CLIs or change global settings.

## Linux or manual terminals

```bash
python3 scripts/setup.py --project /absolute/path/to/project --manual-peer
```

The installed command tells Claude to use `loop-begin --manual-peer`. Open the exact
returned `loop-peer CONVERSATION_ID` command in another terminal. This still connects
one dedicated Codex session and sends replies automatically. Native mode needs a visible
console; `--headless` requires a task explicitly armed with `--read-only-review` or `--propose`.

## Claude command discovery

Open Claude in the configured target directory. If it is already open, inspect `/hooks`
and refresh slash-command discovery as supported by your installed version. A new command
may need a session restart. Inspect existing SessionStart hooks first: they can commit,
push or perform other actions. The bridge never launches another Claude session for setup.

## Diagnostics and removal

```bash
python3 tools/claude-codex-bridge/bridge.py doctor
python3 scripts/setup.py --project /absolute/path/to/project --uninstall
```

Removal deletes only this checkout's managed command and exact hook entries. It retains
runtime evidence and unrelated hooks. Stop any active task with `loop-cancel` first.
If using `--runtime /custom/ignored/directory`, provide the same path on setup, removal
and subsequent bridge commands. Keep that directory out of version control.

## Existing workspace installation

This public checkout can coexist with an older workspace installation **for different
target projects**. Do not install both into the same target: each runtime would observe
the same Claude events. Finish active tasks, remove the old checkout's hook entries and
managed command, then install this checkout. Do not copy live jobs or credentials.
