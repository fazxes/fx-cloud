# fx-cloud

Run builds and tests from your Mac in [Vercel Sandboxes](https://vercel.com/docs/sandbox), one for each agent and folder.

Coding agents such as [fx](https://fx.sh), Claude Code, and Codex run builds and test suites all day. fx-cloud sends those commands to a sandbox and streams the output back, with the same exit code. Your files, git, and the agents themselves stay on the Mac.

| On the Mac | What happens |
| --- | --- |
| `fx-cloud run zig build test` | Copies the folder to this agent's sandbox, runs the command there, and streams the output and exit code back |
| `fx-cloud pull --patch --apply` | Takes the files that the sandbox changed back to the Mac |
| `fx-cloud pull zig-out/bin/app` | Copies a file that a command made in the sandbox |
| `fx-cloud macos-build` | Builds a Zig project's macOS binary in the sandbox and puts it in `zig-out/bin` |
| `fx-cloud done` | Removes this agent's sandbox for the folder |

Each agent gets its own sandbox for each folder it works in. It starts in seconds from a base snapshot that has your toolchain, and it ends by itself when the agent stops using it.

## Install

Requirements:

- macOS or Linux, with Python 3.9 or newer and git
- The [Vercel CLI](https://vercel.com/docs/cli), logged in: `npm i -g vercel`, then `vercel login`
- A Vercel team with Sandbox access, and a project for the sandboxes: `vercel project add <name> --scope <team>`

```sh
git clone https://github.com/fazxes/fx-cloud
cd fx-cloud
./install.sh
fx-cloud init
fx-cloud base
fx-cloud login     # optional
```

1. `./install.sh` checks the requirements, links `fx-cloud` into `~/.local/bin`, and installs the fx-cloud skill for fx (`~/.fx/skills`), Claude Code (`~/.claude/skills`), and agents that read `~/.agents/skills`, for each one whose folder exists. `--add-rule` also adds a three-line rule to `~/.fx/AGENTS.md`, `~/.claude/CLAUDE.md`, and `~/.codex/AGENTS.md`. `--shims` puts the `zig` and `bun` [shims](#shims) first in your PATH.
2. `fx-cloud init` asks for the team and the project (or takes `--team` and `--project`), checks that the Vercel CLI can reach them, and writes `~/.config/fx-cloud/config.json`.
3. `fx-cloud base` builds the base snapshot that every sandbox starts from. It takes about a minute, or 5 to 12 minutes with [warm builds](#warm-caches). The base gets the Zig and Bun versions your Mac uses, fx if you use it, git, ripgrep, jq, and tmux, your skills and global agent rules, your git name and email (no signing key), and your `setup_hook`.
4. `fx-cloud login` signs the base in to GitHub, so sandboxes can fetch your private repositories, and to fx, for `fx-cloud agent`. Each step is optional. It then saves a new base snapshot.

Run `./install.sh` again to update. Run `fx-cloud base` again after your tools or skills change; fx-cloud notes it when the base is more than 3 days old.

## Using it

Agents learn it from the skill: they put `fx-cloud run` in front of heavy commands and keep everything else on the Mac. You can use it the same way, from the project folder:

```sh
fx-cloud run zig build test                     # copy the folder, run there, stream output, same exit code
fx-cloud run 'cd tests/e2e && bun test'
fx-cloud run --include .env npm test            # also copy an ignored file that the command needs
fx-cloud run --no-push sudo apt-get install -y hyperfine   # no copy, for example to install a tool
fx-cloud pull --patch                           # what the sandbox changed in the copy, as a patch
fx-cloud pull --patch --apply                   # apply it to the Mac
fx-cloud pull zig-out/bin/app ./from-sandbox    # copy a file or folder from the sandbox
fx-cloud macos-build                            # macOS binary built in the sandbox, put in ./zig-out/bin
fx-cloud agent "find and fix the flaky test"    # hand a task to fx in the sandbox copy
fx-cloud done                                   # remove this agent's sandbox for this folder
```

```sh
fx-cloud ls              # agent sandboxes: owner, state, age, idle time, folder
fx-cloud status          # settings, the base snapshot, and the number of sandboxes
fx-cloud gc [--all]      # remove sandboxes whose agent or folder is gone (--all: every one)
fx-cloud shell [--base]  # a shell in this agent's sandbox for this folder, or in the base
```

### Settings

`~/.config/fx-cloud/config.json` (`FXC_CONFIG_DIR` moves the folder). `fx-cloud init` writes every key with its default.

| Key | Default | What it does |
| --- | --- | --- |
| `team`, `project` | none | The Vercel team and project for the sandboxes. Required. |
| `prefix` | `fxc-` | The start of every sandbox name that fx-cloud makes. It never touches other sandboxes. |
| `vcpus` | `8` | vCPUs for each sandbox. Each vCPU comes with 2 GB of memory. |
| `idle_minutes` | `30` | How long a sandbox waits for the next command. With 30, it ends 20 to 50 minutes after its last one. |
| `max_agents` | `24` | Above this many agent sandboxes, fx-cloud first removes the ones that no agent needs. |
| `local_only` | `[]` | Repository or folder names where the shims always run the real tool on the Mac, such as projects that build with AppKit. |
| `warm` | `[]` | Repositories that the base clones and builds: `{"repo": "<git url>", "run": ["<command>", ...]}`. |
| `copy_skills` | `true` | Copy your skills folders and global agent rules into the base. |
| `agent_command` | `["fx"]` | What `fx-cloud agent` runs, followed by `ask --json`. |
| `setup_hook` | `null` | The path of your own script, which runs in the base after the standard setup: extra packages, tools, sign-ins. |

`FXC_TEAM`, `FXC_PROJECT`, `FXC_PREFIX`, `FXC_VCPUS`, `FXC_IDLE_MINUTES`, and `FXC_MAX_AGENTS` override the file. `FXC_STATE_DIR` moves the state folder (`~/.cache/fx-cloud`). `FXC_OWNER` names the owner of a sandbox, for example for a script.

### Shims

With `./install.sh --shims`, `zig` and `bun` send their heavy work to the sandbox by themselves, so any agent or terminal that types them gets it without knowing about fx-cloud:

| Command | Runs |
| --- | --- |
| `zig build` (the install step) | In the sandbox, built for the Mac (or for `-Dtarget`). The result is copied back to `zig-out` (or `--prefix`), so `./zig-out/bin/app` runs on the Mac. |
| `zig build <step>`, `zig test` | In the sandbox |
| `bun test`, `bun tests/...`, and `bun x.ts` inside a `tests/` folder | In the sandbox, after a Linux `zig build` of a Zig project and a `bun install` where `node_modules` is missing |
| `zig build run`, `zig fmt`, `zig version`, other `bun` commands | On the Mac |
| Anything in a `local_only` project, and anything with `FXC_LOCAL=1` | On the Mac |

`FXC_SHIM_DRYRUN=1 zig build` prints the decision without running anything.

### Warm caches

Zig keeps almost all of a build in the project's `.zig-cache`, so a new sandbox would build a big project from nothing. List the project in `warm`, and the base builds it the ways you build it:

```json
"warm": [
  {"repo": "https://github.com/you/app.git",
   "run": ["zig build", "zig build -Dtarget=aarch64-macos --prefix zig-out/.fx-cloud-aarch64-macos", "zig build test"]}
]
```

The base keeps the resulting `.zig-cache` as a seed. Before the first command in a copy of the same project (the same `build.zig.zon` fingerprint) that has no `.zig-cache` yet, the sandbox copies the seed in, so only the code that changed since the base compiles again. `zig build` through the shim and `macos-build` install to `zig-out/.fx-cloud-<target>` in the sandbox, so a cross build in `run` should use the same prefix, as above.

Zig compiles `build.zig` into native programs, and sandboxes land on a few different CPU models. So after its first snapshot, `fx-cloud base` starts probe sandboxes until it has one for each other model, builds those programs there, adds them to the seeds, and takes the snapshot again (`--no-cpu-seeds` skips this; `FXC_SEED_CPUS` lists the models). A `warm` repository is also the history source for its copies: a copy then sends only the commits after the base.

### Working on fx

`extras/fx/` holds what fx's own tests expect, outside the core:

- `"setup_hook": "<this folder>/extras/fx/setup.sh"` adds Java 21 with `tla2tools.jar` for the TLA+ model checks, zsh and fish for the shell tests, and CI's tmux defaults to the base.
- `export PATH="<this folder>/extras/fx/shims:$PATH"` sends `sh`, `bash`, and `python3` calls of TLA+ runners under `tests/tla/` (such as `sh tests/tla/<spec>/check.sh`) to the sandbox, through `fx-cloud tla`.

- The copy-back covers TLA+ runner scripts that the `sh`, `bash`, and `python3` shims send to the sandbox through `fx-cloud tla`. A direct `bun` driver run and `fx-cloud run` do not copy logs back yet.
- Each such run gets its own `TMPDIR` in the sandbox, `/tmp/fxc-tla-<uid>/<id>`. When the run ends, its `*.log` and `*.out` files and the drivers' `report.json` files come back to the same path on the Mac, in a folder that only you can read, so the `logs:` and `(see …)` paths that TLC prints work there. Each file is up to 20 MB, and the copy is up to 50 MB in all. The `*.states` and `*.tmp` folders stay in the sandbox.
- fx-cloud prints a note on stderr for each thing it reports: what it copied; each file that did not fit the limits, with the `fx-cloud pull` command to get it, since the run folder stays in the sandbox; each entry it left out of the copy because it was not a plain log file; or why the copy failed. If the run folder cannot be made, the check still runs, and a warning says its logs stay in the sandbox.
- The copy never changes the exit code of the run.

## How it works

- **One sandbox for each agent and folder.** The owner of a command is the nearest fx, Claude Code, or Codex process above it (its process ID and start time), or `FXC_OWNER`, or else the shell. The folder is the git worktree around the current folder. The sandbox is named `<prefix><12 hex digits>`, starts from the base snapshot, and keeps nothing when it ends.
- **Idle time.** A sandbox session starts with `idle_minutes` and gets more while commands run, so a long command keeps its sandbox alive. After the last command, the sandbox ends within 20 to 50 minutes (with the default 30). A background cleanup, at most every 10 minutes, removes the sandboxes whose agent process ended, whose folder is gone, or that stopped.
- **The Mac is the source of truth.** A git worktree's copy lives at the same absolute path in the sandbox. Each run compares the git tree ids of both sides; when they differ, the same call first applies a small git bundle, so the commit and the uncommitted and new files match the Mac. Ignored files stay on the Mac unless you name them with `--include` or list them in `.fx-cloud-include` at the worktree top, and ignored build output stays in the sandbox, so builds are incremental. If something in the sandbox changed the copy, `fx-cloud run` stops and lists the files: `pull --patch --apply` takes them, `--force` discards them.
- **Folders outside git** get a one-way copy, up to 200 MB, without `node_modules`, `.zig-cache`, `zig-out`, and other build folders. Use `pull <path>` to bring files back.
- **Stopping.** A stop signal, such as an agent ending its shell command, also stops the command in the sandbox.
- **The base.** Each `fx-cloud base` starts from a fresh sandbox, because a snapshot holds every disk block that its sandbox ever changed: a fresh one keeps the snapshot small and new sandboxes fast. Sign-ins carry over from the old base. The old base sandbox is removed, and its snapshot is deleted once no sandbox uses it.

Measured in November 2026: a base without warm builds took 33 seconds; with one warm build of fx and the CPU seeds, 7.5 minutes. A new sandbox took 3 seconds to its first output, a run with no change 2 seconds, and a run after an edit on the Mac 1 to 2 seconds. In a copy of fx with the seed, `fx-cloud macos-build` took 12 seconds.

## Costs

Sandboxes bill memory for as long as they live and CPU only while it is busy. On the Pro plan in `iad1` (November 2026), that is $0.0212 per GB-hour of memory and $0.128 per vCPU-hour of active CPU. A sandbox with the default 8 vCPUs has 16 GB, about $0.34 for each hour it lives, plus the CPU time of your builds. Snapshot storage is $0.08 per GB-month; a base without warm builds is about 1.5 GB. Lower `vcpus` and `idle_minutes` to pay less, and see [Vercel's pricing page](https://vercel.com/docs/sandbox/pricing) for current prices and plan limits.

## Safety

- fx-cloud only touches sandboxes whose names start with its `prefix`: `<prefix><12 hex digits>` for agents, `<prefix>base-...` and `<prefix>probe-...` for the base. `gc`, `ls`, and `done --all` never see the others, so it can share a project with other tools.
- It never overwrites a sandbox copy that changed unless you pass `--force`, and `pull --patch --apply` changes nothing on the Mac unless the whole patch applies.
- Sandboxes have no commit signing key and no SSH keys. A note in the sandbox tells agents there not to commit or push; commits stay on the Mac.
- The sign-ins from `fx-cloud login` and your skills and global agent rules (unless `copy_skills` is `false`) are stored in the base snapshot in your Vercel project, where members of that team with Sandbox access can reach them.
- It refuses to copy `/`, `/tmp`, or your home folder.

## Limits

- Sandboxes run Linux on x86_64. Work that needs macOS, such as AppKit, Swift, Xcode, macOS-only tests, or the screen, stays on the Mac; `local_only` keeps the shims away from such projects.
- Warm caches cover the base's commit. The first build after a code change in a new sandbox compiles what changed since then, and a project without a seed builds from nothing in each new sandbox.
- A sandbox on a CPU model that the base has no seed for compiles Zig's build programs again on its first build.
- Ignored files stay on the Mac unless you name them, and folders outside git copy one way only.
- The Hobby plan ends sessions after 45 minutes and has lower quotas than the defaults assume.

## Uninstall

```sh
fx-cloud gc --all        # remove the agent sandboxes
fx-cloud base --remove   # remove the base sandbox and its snapshots
./uninstall.sh           # remove the command, the skill, the shims line, and the rule; keep the settings
./uninstall.sh --purge   # remove the settings and state too
```

`uninstall.sh --purge` stops while a base snapshot is still known, so it is not forgotten in your Vercel project.

## Development

```sh
python3 test/unit.py      # unit tests, no network
test/e2e.sh --local       # install.sh and uninstall.sh in a throwaway home folder
FXC_E2E_TEAM=<team> FXC_E2E_PROJECT=<project> test/e2e.sh   # and a real sandbox, about 2 minutes
```

The live test uses its own settings and state folders and the sandbox prefix `fxt-`. It builds a base without warm builds, runs commands in one agent sandbox, and removes every `fxt-` sandbox and its snapshots at the end, also after a failure. It does not start when `fxt-` sandboxes already exist in the project.

## License

[MIT](LICENSE)
