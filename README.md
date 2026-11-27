# fx-cloud

Send builds, unit tests, E2E tests, dogfood lanes and evals to Vercel Sandboxes, so they use cloud CPUs instead of this Mac. fx stays on the Mac with its files, apps, git, and sessions, and calls `fx-cloud` with its normal shell tool. The rule that tells it when is in `~/.fx/AGENTS.md`, under "Heavy commands run in Vercel Sandboxes".

Each agent gets its own sandbox for each folder it works in. The owner is the nearest fx process above the command (its PID and start time), or `FXC_OWNER` when that is set. The first `fx-cloud run` starts the sandbox from the base snapshot in about 5 s; later runs reuse it. A sandbox ends when the agent runs `fx-cloud done`, after 30 idle minutes (it is non-persistent, so its files go with it), or at the next gc after its fx process ends. A gc runs in the background at most every 10 minutes and logs to `~/.cache/fx-cloud/gc.log`. A running command keeps its sandbox alive however long it takes.

## Use from fx on the Mac

Run these from the project folder:

```bash
fx-cloud run zig build test                   # copy the folder, run there, stream output, same exit code
fx-cloud run 'cd tests/e2e && bun test ./tui-startup.test.ts'
fx-cloud run --include .env npm test          # also copy an ignored file that the command needs
fx-cloud run --no-push sudo apt-get install -y hyperfine   # no copy, for example to install a tool
fx-cloud agent "find and fix the flaky TUI tests"          # hand a task to fx in the sandbox copy
fx-cloud agent --resume <id> "now run the whole TUI suite" # continue that sandbox session
fx-cloud pull --patch --apply                 # take the sandbox copy's changes to the Mac
fx-cloud pull zig-out/bin/fx ./from-sandbox   # copy files from the sandbox copy
fx-cloud macos-build                          # macOS binary built in the sandbox, put in ./zig-out/bin
```

- **Ignored files:** they stay on the Mac. `--include <path>` copies one for a command, and `.fx-cloud-include` at the worktree top lists the ones a project always needs, one per line.
- **Folders outside git:** a one-way copy, up to 200 MB, without `node_modules`, `.zig-cache`, `zig-out`, `__pycache__`, `.venv`, `venv`, `.next`, `.turbo`, `.gradle` and `target`. Only changed files move, and deleted files are deleted in the sandbox. Files that commands make in the sandbox stay. `pull --patch` needs git, so use `pull <path>`. fx-cloud refuses `/`, `/tmp`, and your home folder.
- **macOS binary:** `macos-build` runs `zig build -Dtarget=<arch>-macos --prefix zig-out/macos` in the sandbox copy, so the copy's own `zig-out/bin` keeps its Linux binary for E2E tests. The binary is ad-hoc signed by the Zig linker and runs on the Mac.
- **TLA+:** run `check.sh` scripts, TLC, and Bun drivers through `fx-cloud run`. TLC's working files (date-named `states/` folders, `*_TTrace_*` specs) do not count as changes on either side, and the copy step keeps them, so a stopped or running model check never blocks the next run. A plain `states/` folder of your own still copies. Measured on Nov 11: CI's three TLC checks, three Bun drivers and the build they need passed in 228 s, with 1.3 s of Mac CPU.
- **Session time:** each command reports the session's uptime. When less than 1 hour is left, fx-cloud adds 2 hours, and it checks every 5 minutes while a long command runs. `FXC_TIMEOUT_HOURS` must match the sandbox timeout (5 by default).
- **Quiet commands and lost streams:** while a command runs, the sandbox helper sends a hidden line every 60 seconds, so a command that prints nothing for a long time still keeps its stream active. At the end, the helper sends the command's exit code. If the stream ends before that, fx-cloud says so and exits 77, because the CLI's own code (sometimes 0) tells nothing about the command. The command may still be running in the sandbox.

How the copy works:

- The sandbox keeps a copy of each worktree at the same path. `fx-cloud run` compares git tree ids of both sides, which match when the files match.
- When the files differ, the same sandbox call first applies a small git bundle, so the commit and the uncommitted and new files match the Mac. Ignored files such as `zig-out`, `.zig-cache` and `node_modules` stay in the sandbox, so builds are incremental.
- The Mac is the source of truth. If something in the sandbox changed the copy, `fx-cloud run` stops and lists the files. You then choose: `pull --patch --apply` takes the changes, and `--force` discards them.
- A stop signal, such as fx stopping the shell command, also stops the command in the sandbox.
- The sandbox helper updates itself when `remote/fxc-remote.py` changes.

Measured on Nov 10, from a fresh `origin/main` worktree:

| Step | Wall time | Mac CPU |
| --- | --- | --- |
| `fx-cloud run zig build` (warm cache) | 36 s | 2.4 s |
| `fx-cloud run` of `tui-startup.test.ts` (25 pass, 3 skip) | 47 s | 0.8 s |
| `fx-cloud run` with no change | 3 to 6 s | — |
| `fx-cloud run` after a Mac edit | 5 to 6 s | — |
| `fx-cloud agent --test-lane` small task (5 steps) | 31 s | 1.5 s |
| `fx-cloud macos-build` of fx, then `./zig-out/bin/fx --version` on the Mac | 119 s | 2.5 s |

Each sandbox call costs about 3.5 s in the Vercel CLI alone, so that is the floor for any command. A new agent sandbox adds about 5 s once, and its first build of a worktree is a cold build.

## zig and bun send their heavy work by themselves

`shims/zig` and `shims/bun` come first in PATH (the last line of `~/.zprofile` and `~/.zshrc` adds `~/.local/share/fx-cloud/shims`, a link to `shims/`), so any agent or terminal that types them gets the sandbox without knowing about `fx-cloud`:

| Command | Where |
| --- | --- |
| `zig build` (install step) | Sandbox, built for macOS (or for `-Dtarget`); the result is copied back to `zig-out` (or `--prefix`), so `./zig-out/bin/fx` runs on the Mac |
| `zig build <step>`, `zig test` | Sandbox |
| `bun test`, `bun tests/...`, and `bun x.ts` inside a `tests/` folder | Sandbox, after a Linux `zig build` of the project and a `bun install` where `node_modules` is missing |
| `sh`, `bash` or `python3` with a script under `tests/tla/` of the current git worktree: `sh tests/tla/<spec>/check.sh`, `sh ./check.sh` in its folder, `python3 tests/tla/mcp/run.py ...` | Sandbox (TLC) |
| `zig build run`, `zig fmt`, `zig version`, `zig cc`, `--build-runner` (editor tooling), other bun commands, every other `sh`, `bash` and `python3` call | Mac |
| fxd-zig, fx-desktop, fx-browser (`LOCAL_ONLY_REPOS`), and `/`, `~`, `/tmp` | Mac |
| anything with `FXC_LOCAL=1` | Mac |

`FXC_SHIM_DRYRUN=1 zig build` prints the decision without running anything; the same works for `bun`, and for `sh`, `bash` and `python3` with a `tests/tla/` script.

TLC has three entry points in fx: 30 `tests/tla/*/check.sh` scripts, `tests/tla/mcp/run.py`, and the Bun drivers in `tests/tla/drivers/`. `shims/sh`, `shims/bash` and `shims/python3` catch the first two when an interpreter starts them. They run under `/bin/dash` and use only shell built-ins before they `exec` the real program by its absolute path (about 4–5 ms for `sh` and `bash`, nothing measurable for `python3`); only a `tests/tla/` script calls `fx-cloud tla`. A `check.sh` typed by its path cannot be caught: 26 of them start with `#!/bin/sh`, so macOS starts `/bin/sh` itself. The global rule asks agents to type `sh tests/tla/<spec>/check.sh` instead.

## Agent sandboxes and the base

| Item | Value |
| --- | --- |
| Team / project | `vercel-internal-playground` / `fx-sandbox` |
| Agent sandbox | `fxa-<hash>`: Ubuntu x86_64, 8 vCPUs (`FXC_VCPUS`), 16 GB, 64 GB disk, non-persistent, 30 idle minutes (`FXC_IDLE_MINUTES`) |
| Base | `fxc-base-<time>`, stopped; its snapshot (about 1.5 GiB) is in `~/.config/fx-cloud/base.json` |
| In the base | Zig 0.17.1 (the CI pin and fx's minimum), Bun 1.3.14 (the E2E pin), Java 21 with `tla2tools.jar` v1.7.4 (the CI pin, `TLA2TOOLS_JAR` set), tmux, zsh, fish, fx on the `dev` channel with fx login, the aig test key, gh (to fetch internal repos), the Zig cache seed for fx (below), and a warm Bun cache. No commit signing key: commits stay on the Mac. |
| Workspace | `/Users/faxes/Developer/Fx` in the sandbox too, with `fx/` and `fx-web/` at `origin/main` |
| Cost | memory is billed while a sandbox lives (about $0.34/h for 16 GB), CPU only while busy ($0.128 per vCPU-hour) |

```bash
fx-cloud ls              # agent sandboxes, their owners, state, age, idle time and folder
fx-cloud done [--all]    # remove this agent's sandbox for this folder (or all of its sandboxes)
fx-cloud gc [--all]      # remove sandboxes whose fx process or folder is gone, or that stopped
fx-cloud status          # the base snapshot and the agent sandbox count
fx-cloud base            # build a new base: a fresh sandbox, sign-ins carried over, setup.sh, base.sh, snapshot
fx-cloud login           # sign-ins in the base (fx, GitHub, the aig test key), then a new base snapshot
fx-cloud shell [--base]  # a shell in this agent's sandbox for this folder, or in the base
```

Run `fx-cloud base` after you change skills or `AGENTS.md` on the Mac, or when the base gets old (`fx-cloud` notes it after 3 days). Each build starts from a fresh sandbox, because a snapshot holds every disk block its sandbox ever changed: the first base, a copy of the old shared sandbox, was 13 GiB and took about 60 s to start; a fresh one is about 1.4 GiB and starts in about 5 s. The build copies your skills, `~/.fx/AGENTS.md` without its Mac-only sandbox rule, and the workspace `AGENTS.md` with `remote/AGENTS.sandbox.md` in front. `fx-cloud agent` sessions stay in the agent's sandbox and end with it; the Mac session keeps their answers.

### The Zig cache seed

Zig keeps almost all of a build in the project's `.zig-cache`, so every new agent sandbox would build fx from nothing: about 150 s for `zig build` on Linux and 110–220 s for the macOS one. The base therefore builds fx `origin/main` the ways agents build it (Linux, macOS, `install-test`, one fuzz target) and keeps that `.zig-cache` in `~/.cache/fxc-zig-seed/fx/` (about 1 GB, with `meta.json`: the `build.zig.zon` fingerprint, the commit, the CPU models). Before the first command in a copy of fx without a `.zig-cache`, the sandbox helper copies the seed in (under 1 s) and prints `.zig-cache starts from the warm cache of fx <commit>`. Zig's cache entries do not depend on the folder, so only code that changed compiles again.

| First build in a new sandbox (Nov 2026) | Before | With the seed, same commit | With the seed, one file changed |
| --- | --- | --- | --- |
| `zig build` on the Mac (macOS binary, with the copy back) | 112–224 s | 6–10 s | about 100 s |
| `zig build` on Linux | 149 s | 0 s on the base's CPU, 20 s on others | 20–25 s |

Three things decide whether the seed matches:

- **The CPU model.** Before every build, Zig compiles `build.zig` into two native programs (the configurer and the maker, 95–130 s), and native code holds the sandbox's CPU model. Sandboxes land on `cascadelake`, `skylake_avx512` and `icelake_server` (`FXC_SEED_CPUS`). So after its first snapshot, `fx-cloud base` starts probe sandboxes from it until it has one for each other model, builds those two programs there (about 9 MB each), adds them to the seed, and takes the snapshot again. `--no-cpu-seeds` skips this. A native Linux build of fx itself stays per CPU, but any code change compiles it again anyway.
- **The install folder.** fx's `build.zig` puts the install folder into the `test_paths` options that the fx module imports, so a new folder means a new compile. `zig build` from the Mac and `macos-build` therefore always install to `zig-out/.fx-cloud-<target>` in the sandbox, which the base warms too.
- **`tmp/`.** Zig fails with `configuration failed: FileNotFound` when a cache that it did not make has no `tmp/` folder, so the seed and each copy keep an empty one.

`FXC_SANDBOX=<name>` sends every command to one named sandbox instead, as before; `fx-dev` is the old shared sandbox. Until a base exists, commands also fall back to `fx-dev`.

The review skill runs its hard checks through `fx-cloud` when `~/.review/runner.json` names it, with one owner per review (`FXC_OWNER=review-<review>`), so its checks and the agent's `review run ... -- fx-cloud run ...` share each worktree's sandbox. `REVIEW_LOCAL=1` runs them on the Mac.

## What stays on the Mac

`fx-prep/`, `docs/`, `reviews/`, `pipeline-harness/`, `parked-sessions/`, `fxd-zig/` and `fx-browser/`, and work that needs AppKit, Swift, Xcode, or macOS-only tests.

## Settings

`FXC_TEAM`, `FXC_PROJECT`, `FXC_VCPUS`, `FXC_IDLE_MINUTES`, `FXC_MAX_AGENTS` (a warning, not a limit), `FXC_OWNER`, `FXC_SANDBOX`, `FXC_REMOTE_WS` and `FXC_TIMEOUT_HOURS` (pinned sandboxes only) override the defaults.
