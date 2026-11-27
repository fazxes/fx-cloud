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
import json
import os
import shlex
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
import zlib

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
BYTES_PER_KILOBYTE = 1024
BYTES_PER_MEGABYTE = 1024 * BYTES_PER_KILOBYTE
MAX_LOG_FILE_BYTES = 20 * BYTES_PER_MEGABYTE  # must equal LOG_FILE_LIMIT_BYTES in remote/fxc-remote.py; a unit test checks it
MAX_LOG_TOTAL_BYTES = 50 * BYTES_PER_MEGABYTE  # must equal LOG_TOTAL_LIMIT_BYTES in remote/fxc-remote.py; a unit test checks it
STALE_DOWNLOAD_SECONDS = 600  # a run killed mid-copy leaves its logs-*.tgz behind
REASON_MAX_CHARACTERS = 200


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
    return ('d=%s; if mkdir -p "$d"; then export TMPDIR="$d"; else echo "fx-cloud: could not make $d, so the logs of this run stay in the sandbox" >&2; fi; %s; rc=$?; { %s; } >/dev/null 2>&1; exit $rc'
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


def is_skipped_entry(entry):
    if not isinstance(entry, dict):
        return False
    path, size = entry.get("path"), entry.get("bytes")
    return isinstance(path, str) and isinstance(size, int) and not isinstance(size, bool)


def read_manifest(source):
    try:
        entries = json.loads(source.read().decode("utf-8"))
    except ValueError:
        return []
    if not isinstance(entries, list):
        return []
    return [(entry["path"], entry["bytes"]) for entry in entries if is_skipped_entry(entry)]


def write_log(source, target):
    os.makedirs(os.path.dirname(target), mode=PRIVATE_DIR_MODE, exist_ok=True)
    with open(target, "wb") as out:
        shutil.copyfileobj(source, out)
    os.chmod(target, PRIVATE_FILE_MODE)


def member_target(root, member):
    """The real path where MEMBER would be written under ROOT, or None when it leaves ROOT."""
    target = os.path.realpath(os.path.join(root, member.name))
    return target if is_inside(root, target) else None


def fits_limits(size, written_bytes):
    return size <= MAX_LOG_FILE_BYTES and written_bytes + size <= MAX_LOG_TOTAL_BYTES


def extract_folder(root, member):
    target = member_target(root, member)
    if target is not None:
        os.makedirs(target, mode=PRIVATE_DIR_MODE, exist_ok=True)


def extract_log(tar, member, root, written_bytes):
    """Writes a regular file under ROOT when it fits the limits. Returns True when it was written."""
    target = member_target(root, member)
    if target is None or not member.isreg() or not fits_limits(member.size, written_bytes):
        return False
    with tar.extractfile(member) as source:
        write_log(source, target)
    return True


def extract_logs(tgz, dest):
    count, refused, written_bytes, skipped = 0, 0, 0, []
    try:
        os.makedirs(dest, mode=PRIVATE_DIR_MODE, exist_ok=True)
        root = os.path.realpath(dest)
        with tarfile.open(tgz, "r:gz") as tar:
            for member in tar:
                if member.name == SKIPPED_MANIFEST and member.isreg():
                    skipped = read_manifest(tar.extractfile(member))
                elif member.isdir():
                    extract_folder(root, member)
                elif extract_log(tar, member, root, written_bytes):
                    count += 1
                    written_bytes += member.size
                else:
                    refused += 1
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        return count, skipped, refused, printable(str(e) or type(e).__name__)
    return count, skipped, refused, None


def printable(text):
    return "".join(ch if ch.isprintable() else escape_char(ch) for ch in text)


def escape_char(ch):
    code = ord(ch)
    return "\\x%02x" % code if code < 256 else "\\u%04x" % code


def shell_escape_char(ch):
    if ch == "\\":
        return "\\\\"
    if ch == "'":
        return "\\'"
    if ch.isprintable():
        return ch
    return "".join("\\x%02x" % byte for byte in ch.encode("utf-8"))


def shell_quote_printable(text):
    if text.isprintable():
        return shlex.quote(text)
    return "$'" + "".join(shell_escape_char(ch) for ch in text) + "'"


def human_size(byte_count):
    if byte_count >= BYTES_PER_MEGABYTE:
        return "%.1f MB" % (byte_count / BYTES_PER_MEGABYTE)
    if byte_count >= BYTES_PER_KILOBYTE:
        return "%d KB" % max(1, byte_count // BYTES_PER_KILOBYTE)
    return "%d bytes" % byte_count


def skipped_note(path_and_bytes, run_dir):
    relpath, byte_count = path_and_bytes
    full = run_dir + "/" + relpath
    return "%s is too large to copy back (%s); get it with: fx-cloud pull %s <dest>" % (
        printable(full), human_size(byte_count), shell_quote_printable(full))


def refused_note(refused, run_dir):
    return "left out %d %s from the sandbox that %s not plain log files under %s" % (
        refused, "entry" if refused == 1 else "entries", "was" if refused == 1 else "were", run_dir)


def log_notes(count, skipped, refused, run_dir):
    notes = []
    if count > 0:
        notes.append("copied %d TLC log %s to %s" % (count, "file" if count == 1 else "files", run_dir))
    notes.extend(skipped_note(entry, run_dir) for entry in skipped)
    if refused > 0:
        notes.append(refused_note(refused, run_dir))
    return notes


def remove_quietly(path):
    with contextlib.suppress(OSError):
        os.remove(path)


def failure_reason(stderr, returncode):
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "exit %d" % returncode
    return printable(lines[-1])[:REASON_MAX_CHARACTERS]


def fetch_logs(core, run_dir, tgz):
    """Copies the run's tarball from the sandbox to TGZ. Returns (ok, reason); reason is "" when ok."""
    try:
        done = core.subprocess.run(core.vercel("copy", "%s:%s.tgz" % (core.SANDBOX, run_dir), tgz),
                                   stdout=core.subprocess.DEVNULL, stderr=core.subprocess.PIPE, text=True)
    except OSError as e:
        return False, printable(str(e) or type(e).__name__)
    if done.returncode == 0:
        return True, ""
    return False, failure_reason(done.stderr, done.returncode)


def stale_downloads(entries, now):
    return [name for name, mtime in entries
            if name.startswith("logs-") and name.endswith(".tgz") and now - mtime > STALE_DOWNLOAD_SECONDS]


def remove_stale_downloads(parent):
    entries = []
    with contextlib.suppress(OSError):
        with os.scandir(parent) as listing:
            for entry in listing:
                if entry.is_file(follow_symlinks=False):
                    entries.append((entry.name, entry.stat(follow_symlinks=False).st_mtime))
    for name in stale_downloads(entries, time.time()):
        remove_quietly(os.path.join(parent, name))


def bring_logs_back(core, run_dir, uid):
    if not core.SANDBOX:
        return
    parent = os.path.dirname(run_dir)
    if not prepare_logs_parent(parent, uid):
        core.say("not copying the TLC logs back: %s is not a private folder of yours" % parent)
        return
    remove_stale_downloads(parent)
    try:
        fd, tgz = tempfile.mkstemp(prefix="logs-", suffix=".tgz", dir=parent)
        os.close(fd)
    except OSError as e:
        core.say("could not copy the TLC logs back: %s" % e)
        return
    try:
        ok, reason = fetch_logs(core, run_dir, tgz)
        if not ok:
            core.say("could not copy the TLC logs back from the sandbox: %s; get them with: fx-cloud pull %s ." % (
                reason, shell_quote_printable(run_dir + ".tgz")))
            return
        count, skipped, refused, error = extract_logs(tgz, run_dir)
    finally:
        remove_quietly(tgz)
    if error:
        core.say("could not unpack the TLC logs: %s" % error)
    for note in log_notes(count, skipped, refused, run_dir):
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
    # The run's exit code must stay the check's own, so a broken copy-back only prints a note.
    try:
        bring_logs_back(core, run_dir, uid)
    except Exception as e:
        core.say("could not copy the TLC logs back: %s" % printable(str(e) or type(e).__name__))
    return rc
