# fx cloud sandbox

This machine is a Vercel Sandbox that works for one fx agent on the user's Mac; it is not the Mac. fx on the Mac sends heavy commands and handoff tasks here with `fx-cloud`, so the Mac stays cool. When you run here, fx on the Mac handed you the task with `fx-cloud agent`. The workspace rules below this section still apply, with these differences:

- **Platform:** Linux x86_64 (Ubuntu), 8 vCPUs, 16 GB memory. Builds, unit tests, E2E tests, dogfood lanes, harness runs and evals all run here. Run the focused tests here as the fx `AGENTS.md` says.
- **macOS work:** AppKit, `fxd-zig`, the Swift app, macOS PGSO and hands-on macOS checks cannot run here. Say so in your report; fx on the Mac handles them.
- **What is here:** `fx/` and `fx-web/` (canonical checkouts, same rules as on the Mac) and `worktrees/`. `fx-prep/`, `docs/`, `reviews/`, `pipeline-harness/`, `parked-sessions/`, `fxd-zig/` and `fx-browser/` stay on the Mac and are not here. If a task needs one of them, say so.
- **Copies of Mac worktrees:** `fx-cloud` keeps a copy of each Mac worktree it uses at the same path here, and the Mac is their source of truth. Leave your changes uncommitted in the copy; the Mac takes them back with `fx-cloud pull --patch`. Do not commit, push, or reset in these copies.
- **Git:** the work identity is set and `gh` can fetch, but there is no commit signing key. Do not commit, push or open PRs here; fx on the Mac does that.
- **Billing lanes:** `~/.local/bin/aig` works here as on the Mac. If `aig test` reports a missing key, stop and say so.
- **Lifetime:** this sandbox lives while its agent works. It ends after a while without commands, or when the agent runs `fx-cloud done`, and then its files and processes are gone. Anything that must stay goes back to the Mac with `fx-cloud pull`.
- **tmux:** E2E tests use their own tmux servers and the default `~/.tmux.conf`, which matches CI. Do not change that file.

