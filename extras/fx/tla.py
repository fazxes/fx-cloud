"""`fx-cloud tla`: fx's TLA+ runners in the sandbox.

The sh, bash and python3 shims in extras/fx/shims call `fx-cloud tla --tool <tool> --real <path>
-- <args>` only when a call looks like a TLA+ runner. This module decides: a script under
tests/tla/ of the current git worktree, such as `sh tests/tla/<spec>/check.sh` or
`python3 tests/tla/mcp/run.py`, runs in the agent's sandbox; anything else runs the real program
here. fx-cloud loads this file for its `tla` command; `core` is the fx-cloud module.
"""
import os
import shlex

# The interpreters' own options that may come before the script; anything else (such as -c) means
# that no TLA+ runner script is given, so the call runs on the Mac.
TLA_OPTS = {
    "sh": {"-e", "-x", "-u", "-v", "-eu", "-ue", "-ex", "-xe", "-eux", "-xeu"},
    "bash": {"-e", "-x", "-u", "-v", "-eu", "-ue", "-ex", "-xe", "-eux", "-xeu"},
    "python3": {"-u", "-B", "-E", "-s", "-S", "-O"},
}


def git_toplevel(core, path):
    p = core.subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return os.path.realpath(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None


def tla_route(core, tool, args):
    """remote-run when TOOL runs a script under tests/tla/ of the current git worktree; else local."""
    i = 0
    while i < len(args) and args[i] in TLA_OPTS.get(tool, ()):
        i += 1
    if i >= len(args) or args[i].startswith("-"):
        return "local"
    script = args[i]
    if not script.endswith(".py" if tool == "python3" else ".sh") or not os.path.isfile(script):
        return "local"
    top = git_toplevel(core, os.getcwd())
    if not top:
        return "local"
    rel = os.path.relpath(os.path.realpath(script), top)
    return "remote-run" if rel.startswith("tests/tla/") else "local"


def main(core, args):
    flags, rest = core.take_flags(args, (), valued=("--real", "--tool"))
    tool, real = flags.get("--tool", ""), flags.get("--real")
    if tool not in TLA_OPTS or not real or not os.access(real, os.X_OK):
        core.die("the shim found no real %s; set FXC_LOCAL=1 or check PATH" % (tool or "program"), 127)
    route = tla_route(core, tool, rest)
    if route != "local" and not core.project_allows_remote(os.getcwd()):
        route = "local"
    if os.environ.get("FXC_SHIM_DRYRUN"):
        print("%s %s -> %s" % (tool, shlex.join(rest), route))
        return 0
    if route == "local":
        os.execv(real, [real] + rest)  # the absolute path, so Python finds its own prefix
    core.need_config(tool)
    core.say("%s runs in this agent's sandbox: TLC runs there, not on this Mac" % shlex.join([tool] + rest[:2]))
    rc, _ = core.run_here(shlex.join([tool] + rest), {})
    return rc
