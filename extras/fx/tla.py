"""`fx-cloud tla`: fx's TLA+ runners in the sandbox.

The sh, bash and python3 shims in extras/fx/shims call `fx-cloud tla --tool <tool> --real <path>
-- <args>` only when a call looks like a TLA+ runner. This module decides: a script under
tests/tla/ of the current git worktree, such as `sh tests/tla/<spec>/check.sh` or
`python3 tests/tla/mcp/run.py`, runs in the agent's sandbox; anything else runs the real program
here. Each remote run gets its own TMPDIR in the sandbox, and its log files come back to the same
path on the Mac, so the paths that TLC prints work there. fx-cloud loads this file for its `tla`
command; `core` is the fx-cloud module.
"""
import contextlib
import os
import shlex
import shutil
import stat
import tarfile
import tempfile
import uuid

# The interpreters' own options that may come before the script; anything else (such as -c) means
# that no TLA+ runner script is given, so the call runs on the Mac.
TLA_OPTS = {
    "sh": {"-e", "-x", "-u", "-v", "-eu", "-ue", "-ex", "-xe", "-eux", "-xeu"},
    "bash": {"-e", "-x", "-u", "-v", "-eu", "-ue", "-ex", "-xe", "-eux", "-xeu"},
    "python3": {"-u", "-B", "-E", "-s", "-S", "-O"},
}

# The run folder has the same path in the sandbox and on the Mac, so the paths that TLC prints work there.
LOGS_ROOT_FORMAT = "/tmp/fxc-tla-%d"
RUN_ID_CHARS = 8
SKIPPED_MANIFEST = ".fxc-skipped"  # must equal the one in remote/fxc-remote.py; a unit test checks it
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
BYTES_PER_MEGABYTE = 1024 * 1024


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


def run_dir_path(uid, run_id):
    return LOGS_ROOT_FORMAT % uid + "/" + run_id


def remote_script(command, run_dir, pack_command):
    return ('d=%s; mkdir -p "$d" && export TMPDIR="$d"; %s; rc=$?; { %s; } >/dev/null 2>&1; exit $rc'
            % (shlex.quote(run_dir), command, pack_command))


def prepare_logs_parent(path, uid):
    try:
        os.makedirs(path, mode=PRIVATE_DIR_MODE, exist_ok=True)
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        return False
    try:
        os.chmod(path, PRIVATE_DIR_MODE)
    except OSError:
        return False
    return True


def is_inside(root, target):
    return target == root or target.startswith(root + os.sep)


def read_manifest(source):
    text = source.read().decode(errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def write_log(source, target):
    os.makedirs(os.path.dirname(target), mode=PRIVATE_DIR_MODE, exist_ok=True)
    with open(target, "wb") as out:
        shutil.copyfileobj(source, out)
    os.chmod(target, PRIVATE_FILE_MODE)


def extract_member(tar, member, root):
    """Writes one member under ROOT. Returns True when it was a log file."""
    target = os.path.realpath(os.path.join(root, member.name))
    if not is_inside(root, target):
        return False
    if member.isdir():
        os.makedirs(target, mode=PRIVATE_DIR_MODE, exist_ok=True)
    elif member.isreg():
        with tar.extractfile(member) as source:
            write_log(source, target)
        return True
    return False


def extract_logs(tgz, dest):
    count, skipped_lines = 0, []
    try:
        os.makedirs(dest, mode=PRIVATE_DIR_MODE, exist_ok=True)
        root = os.path.realpath(dest)
        with tarfile.open(tgz, "r:gz") as tar:
            for member in tar:
                if member.name == SKIPPED_MANIFEST and member.isreg():
                    skipped_lines = read_manifest(tar.extractfile(member))
                elif extract_member(tar, member, root):
                    count += 1
    except (tarfile.TarError, OSError, EOFError) as e:
        return count, skipped_lines, str(e) or type(e).__name__
    return count, skipped_lines, None


def skipped_note(line, run_dir):
    relpath, _, size_text = line.partition("\t")
    if not size_text.isdecimal():
        return "could not read a skipped log entry: %s" % line
    path = run_dir + "/" + relpath
    return "%s is too large to copy (%d MB); get it with: fx-cloud pull %s <dest>" % (
        path, int(size_text) // BYTES_PER_MEGABYTE, path)


def log_notes(count, skipped_lines, run_dir):
    notes = []
    if count > 0:
        notes.append("copied %d TLC log %s to %s" % (count, "file" if count == 1 else "files", run_dir))
    notes.extend(skipped_note(line, run_dir) for line in skipped_lines)
    return notes


def remove_quietly(path):
    with contextlib.suppress(OSError):
        os.remove(path)


def fetch_logs(core, run_dir, tgz):
    try:
        rc = core.subprocess.call(core.vercel("copy", "%s:%s.tgz" % (core.SANDBOX, run_dir), tgz),
                                  stdout=core.subprocess.DEVNULL, stderr=core.subprocess.DEVNULL)
    except OSError:
        return False
    return rc == 0


def bring_logs_back(core, run_dir, uid):
    if not core.SANDBOX:
        return
    parent = os.path.dirname(run_dir)
    if not prepare_logs_parent(parent, uid):
        core.say("not copying the TLC logs back: %s is not a private folder of yours" % parent)
        return
    try:
        fd, tgz = tempfile.mkstemp(prefix="logs-", suffix=".tgz", dir=parent)
        os.close(fd)
    except OSError as e:
        core.say("could not copy the TLC logs back: %s" % e)
        return
    try:
        if not fetch_logs(core, run_dir, tgz):
            core.say("could not copy the TLC logs back from the sandbox (%s.tgz)" % run_dir)
            return
        count, skipped_lines, error = extract_logs(tgz, run_dir)
    finally:
        remove_quietly(tgz)
    if error:
        core.say("could not unpack the TLC logs: %s" % error)
    for note in log_notes(count, skipped_lines, run_dir):
        core.say(note)


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
    uid = os.getuid()
    run_dir = run_dir_path(uid, uuid.uuid4().hex[:RUN_ID_CHARS])
    pack_command = core.helper_script("packlogs", run_dir, run_dir + ".tgz")
    rc, _ = core.run_here(remote_script(shlex.join([tool] + rest), run_dir, pack_command), {})
    bring_logs_back(core, run_dir, uid)
    return rc
