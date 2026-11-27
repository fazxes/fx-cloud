#!/usr/bin/env python3
"""Sandbox half of fx-cloud. The Mac calls it through `vercel sandbox exec`.

Worktree copies (every op starts with the protocol version):
  run V TOP CWD SHA TREE CWIP INCLUDES PIDFILE SCRIPT
                                run SCRIPT in the copy of TOP, but only when the copy matches the Mac
                                (HEAD == SHA and the working tree's tree id == TREE); else exit 75
  apply V TOP SHA TREE CWIP INCLUDES ORIGIN BUNDLE EXCLUDE FORCE
                                make the copy of TOP match the Mac commit CWIP (HEAD stays SHA);
                                INCLUDES names ignored files that the copy also carries
  patch V TOP                   binary patch of the copy's changes since the last apply, on stdout
  changes V TOP                 the changed paths since the last apply
  tar V DIR PATH                tarball of PATH (relative to DIR) for the Mac to download
  packlogs V DIR OUT            the log files under DIR (*.log, *.out, *report.json; not in *.states or *.tmp folders; size-limited) as a tarball at OUT, plus a list of the ones left out
  runplain V ROOT CWD DIGEST PIDFILE SCRIPT
  applyplain V ROOT DIGEST BASE TAR DELETES
                                the same for a folder that is not a git worktree: a one-way copy

For base.sh (no protocol version):
  repo URL                      make the repository that holds the objects of URL's copies, and
                                print its path

Results for the Mac go to stderr as one line: FXC::<json>.
"""
import base64
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time

with open(os.path.abspath(__file__), "rb") as _self:
    # The Mac sends the hash of its copy of this file; any difference makes it upload the new one.
    PROTOCOL = hashlib.sha256(_self.read()).hexdigest()[:12]
NEEDS_PUSH = 75
OLD_HELPER = 76
# One repository for each origin URL. It holds the objects of every copy of that project, and
# base.sh clones the `warm` repositories here, so a copy needs only the commits after the base.
REPOS = os.path.expanduser("~/.cache/fx-cloud/repos")
MARKER = "fx-cloud-pushed.json"
# TLC's own working files: state folders named by date and time, and trace specs. They are not
# part of the Mac's work, so neither side counts them, and `git clean` keeps them, so a running
# model check is not disturbed. The Mac script has the same lists; keep them equal.
TLC_PATHSPECS = [":(exclude,glob)**/states/[0-9][0-9]-[0-9][0-9]-[0-9][0-9]-*/**",
                 ":(exclude,glob)**/*_TTrace_[0-9]*.tla", ":(exclude,glob)**/*_TTrace_[0-9]*.bin"]
TLC_CLEAN_KEEP = ["states/[0-9][0-9]-[0-9][0-9]-[0-9][0-9]-*/", "*_TTrace_[0-9]*"]
# Warm .zig-cache folders that base.sh made from the `warm` builds, one per repository:
# <name>/cache and <name>/meta.json with the build.zig.zon fingerprint and the commit.
ZIG_SEEDS = os.path.expanduser("~/.cache/fxc-zig-seed")
# These pick the log files that `fx-cloud tla` copies back to the Mac. TLC state folders (*.states)
# and Java temp folders (*.tmp) can be huge, so they stay in the sandbox. The drivers' report.json
# comes back too; their other JSON files are large state graphs.
LOG_SUFFIXES = (".log", ".out", "report.json")
SKIPPED_DIR_SUFFIXES = (".states", ".tmp")
LOG_FILE_LIMIT_BYTES = 20 * 1024 * 1024
LOG_TOTAL_LIMIT_BYTES = 50 * 1024 * 1024
SKIPPED_MANIFEST = ".fxc-skipped"


def emit(obj):
    sys.stderr.write("FXC::" + json.dumps(obj) + "\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- git helpers

class GitError(Exception):
    pass


def git(cwd, *args, env=None, check=True, data=None):
    p = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, env=env, input=data)
    if check and p.returncode != 0:
        raise GitError("git %s: %s" % (" ".join(args[:3]), p.stderr.decode(errors="replace").strip()[-800:]))
    return p


def git_out(cwd, *args, env=None):
    return git(cwd, *args, env=env).stdout.decode().strip()


def is_worktree(top):
    if not os.path.isdir(top):
        return False
    p = git(top, "rev-parse", "--show-toplevel", check=False)
    return p.returncode == 0 and os.path.realpath(p.stdout.decode().strip()) == os.path.realpath(top)


def has_commit(repo, rev):
    return git(repo, "cat-file", "-e", rev + "^{commit}", check=False).returncode == 0


def worktree_tree(top, includes=()):
    """Tree id of the files as `git add -A` would stage them (tracked and untracked, not ignored),
    plus INCLUDES: ignored files that the Mac asked to copy."""
    gitdir = git_out(top, "rev-parse", "--absolute-git-dir")
    index = git_out(top, "rev-parse", "--git-path", "index")
    if not os.path.isabs(index):
        index = os.path.join(top, index)
    tmp = os.path.join(gitdir, "fx-cloud-index.%d" % os.getpid())
    try:
        if os.path.exists(index):
            shutil.copy2(index, tmp)  # keeps its time, so git still re-reads racily clean files
        env = dict(os.environ, GIT_INDEX_FILE=tmp)
        git(top, "add", "-A", "--", ".", *TLC_PATHSPECS, env=env)
        present = [p for p in includes if os.path.lexists(os.path.join(top, p))]
        if present:
            git(top, "add", "-f", "--pathspec-from-file=-", "--pathspec-file-nul", env=env,
                data="\0".join(present).encode())
        return git_out(top, "write-tree", env=env)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def decode_list(arg):
    """A list from the Mac: "-" for none, or b64:<NUL-separated paths>."""
    if arg in ("-", ""):
        return []
    return [p for p in base64.b64decode(arg[4:]).decode("utf-8").split("\0") if p]


def session_clock():
    """Seconds since this sandbox session booted, which the Mac uses to extend the session in time."""
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot = f.read().strip()
        return {"uptime": uptime, "boot": boot}
    except OSError:
        return {}


def marker_path(top):
    return os.path.join(git_out(top, "rev-parse", "--absolute-git-dir"), MARKER)


def read_marker(top):
    try:
        with open(marker_path(top)) as f:
            return json.load(f)
    except (OSError, ValueError, GitError):
        return None


def write_marker(top, sha, tree, cwip, includes):
    path = marker_path(top)
    with open(path + ".tmp", "w") as f:
        json.dump({"sha": sha, "tree": tree, "cwip": cwip, "includes": includes}, f)
    os.replace(path + ".tmp", path)


def name_status(top, a, b):
    out = git_out(top, "diff", "--name-status", "--no-renames", a, b)
    return [line for line in out.splitlines() if line]


def ref_key(top):
    return hashlib.sha1(os.path.realpath(top).encode()).hexdigest()[:12]


def https_url(url):
    """The URL the sandbox fetches from: SSH remotes become HTTPS, because the sandbox has no SSH
    keys; `gh auth setup-git` signs HTTPS fetches in to GitHub."""
    url = url.strip().rstrip("/")
    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", url)
    if m:
        url = "https://%s/%s" % (m.group(1), m.group(2))
    if url.startswith("https://github.com/") and not url.endswith(".git"):
        url += ".git"
    return url


def repo_slug(url):
    """The folder name of URL's repository: host and path, such as github.com__owner__name."""
    rest = re.sub(r"^[a-z+]+://", "", url)
    rest = re.sub(r"\.git$", "", rest)
    return re.sub(r"[^A-Za-z0-9._-]+", "__", rest).strip("_") or "repo"


def ensure_dir(path):
    """Makes a folder the sandbox user owns, with sudo for paths such as /private/tmp."""
    if os.path.isdir(path):
        return
    try:
        os.makedirs(path)
    except PermissionError:
        subprocess.run(["sudo", "mkdir", "-p", path], check=True)
        subprocess.run(["sudo", "chown", "%d:%d" % (os.getuid(), os.getgid()), path], check=True)


def repo_for(url):
    """The bare repository for origin URL, made when it does not exist yet."""
    os.makedirs(REPOS, exist_ok=True)
    path = os.path.join(REPOS, repo_slug(url) + ".git")
    if not os.path.isdir(path):
        subprocess.run(["git", "init", "-q", "--bare", path], check=True)
        git(path, "remote", "add", "origin", url)
        git(path, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    return path


def ensure_base(top, origin):
    """The repository that holds the objects for the copy of TOP."""
    if is_worktree(top):
        return top
    if origin:
        return repo_for(https_url(origin))
    os.makedirs(REPOS, exist_ok=True)
    path = os.path.join(REPOS, "local-" + ref_key(top) + ".git")
    if not os.path.isdir(path):
        subprocess.run(["git", "init", "-q", "--bare", path], check=True)
    return path


def write_exclude(repo, path):
    """Copies the Mac's ignore rules into the repository, so both sides ignore the same files."""
    common = git_out(repo, "rev-parse", "--git-common-dir")
    if not os.path.isabs(common):
        common = os.path.join(repo, common)
    target = os.path.join(common, "info", "exclude")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    start, end = "# >>> fx-cloud: ignore rules from the Mac", "# <<< fx-cloud"
    old = open(target).read() if os.path.exists(target) else ""
    if start in old and end in old:
        old = old[: old.index(start)] + old[old.index(end) + len(end):]
    with open(path) as f:
        mac = f.read()
    with open(target, "w") as f:
        f.write(old.rstrip("\n") + "\n" + start + "\n" + mac.rstrip("\n") + "\n" + end + "\n")


def zig_fingerprint(path):
    try:
        with open(path) as f:
            m = re.search(r"\.fingerprint\s*=\s*(0x[0-9a-fA-F]+)", f.read())
    except OSError:
        return None
    return m.group(1).lower() if m else None


def seed_zig_cache(top):
    """Gives a copy with no .zig-cache yet the warm cache of its project, so its first build
    compiles only what changed since the base. Zig's cache entries do not depend on the folder."""
    dest = os.path.join(top, ".zig-cache")
    if os.path.exists(dest) or not os.path.isdir(ZIG_SEEDS):
        return
    want = zig_fingerprint(os.path.join(top, "build.zig.zon"))
    if not want:
        return
    for name in sorted(os.listdir(ZIG_SEEDS)):
        seed = os.path.join(ZIG_SEEDS, name)
        try:
            with open(os.path.join(seed, "meta.json")) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if meta.get("fingerprint") != want or not os.path.isdir(os.path.join(seed, "cache")):
            continue
        start = time.time()
        tmp = "%s.seed-%d" % (dest, os.getpid())
        p = subprocess.run(["cp", "-a", "--reflink=auto", os.path.join(seed, "cache"), tmp],
                           capture_output=True)
        if p.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            sys.stderr.write("fx-cloud: could not copy the warm Zig cache, so this build starts cold: %s\n"
                             % p.stderr.decode(errors="replace").strip()[-300:])
            return
        # Zig expects tmp/ in a cache it did not make itself ("configuration failed: FileNotFound").
        os.makedirs(os.path.join(tmp, "tmp"), exist_ok=True)
        try:
            os.rename(tmp, dest)
        except OSError:  # a parallel command of the same agent was first
            shutil.rmtree(tmp, ignore_errors=True)
            return
        sys.stderr.write("fx-cloud: .zig-cache starts from the warm cache of %s %s (copied in %.1f s)\n"
                         % (meta.get("name", name), meta.get("commit", "?"), time.time() - start))
        return


# --------------------------------------------------------------------------- worktree ops

def spawn(run_dir, pidfile, script):
    """Runs SCRIPT in its own process group, records the group for a stop from the Mac, and
    returns its exit code."""
    emit(session_clock())
    os.makedirs(os.path.dirname(pidfile), exist_ok=True)
    child = subprocess.Popen(["bash", "-lc", script], cwd=run_dir, start_new_session=True)
    with open(pidfile, "w") as f:
        f.write(str(child.pid))

    def stop(signum, _frame):
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, stop)
    rc = child.wait()
    try:
        os.unlink(pidfile)
    except OSError:
        pass
    return rc if rc >= 0 else 128 - rc


def op_run(top, cwd, sha, tree, cwip, includes, pidfile, script):
    if top == "-":
        return spawn(cwd if os.path.isdir(cwd) else os.path.expanduser("~"), pidfile, script)
    inc = decode_list(includes)
    if not is_worktree(top):
        emit({"needs_push": True, "reason": "no copy yet", "has_cwip": False})
        return NEEDS_PUSH
    head = git_out(top, "rev-parse", "HEAD")
    now = worktree_tree(top, inc)
    if head != sha or now != tree:
        emit({"needs_push": True, "reason": "commit differs" if head != sha else "files differ",
              "has_cwip": has_commit(top, cwip)})
        return NEEDS_PUSH
    mark = read_marker(top)
    if not mark or mark.get("tree") != now or mark.get("sha") != sha or mark.get("includes") != inc:
        write_marker(top, sha, now, cwip, inc)  # for example after the Mac applied a pulled patch
    seed_zig_cache(top)
    return spawn(cwd if os.path.isdir(cwd) else top, pidfile, script)


def inline_file(arg, name):
    """Small payloads come inline as b64:<data>; they go to a private temporary file."""
    if not arg.startswith("b64:"):
        return arg
    path = os.path.join("/tmp", "fxc-%s-%d" % (name, os.getpid()))
    with open(path, "wb") as f:
        f.write(base64.b64decode(arg[4:]))
    os.chmod(path, 0o600)
    return path


def drop_inline(*paths):
    for path in paths:
        if os.path.basename(path).startswith("fxc-") and path.startswith("/tmp/fxc-") and \
                path.rsplit("-", 1)[-1] == str(os.getpid()):
            os.unlink(path)


def op_apply(top, sha, tree, cwip, includes, origin, bundle, exclude, force):
    bundle, exclude = inline_file(bundle, "bundle"), inline_file(exclude, "exclude")
    try:
        return apply_copy(top, sha, tree, cwip, decode_list(includes), origin, bundle, exclude, force)
    finally:
        drop_inline(bundle, exclude)


def apply_copy(top, sha, tree, cwip, inc, origin, bundle, exclude, force):
    base = ensure_base(top, origin)
    if exclude != "-":
        write_exclude(base, exclude)
    key = "refs/fx-cloud/" + ref_key(top)
    if bundle != "-":
        p = git(base, "fetch", "-q", bundle, "+%s:%s" % (key, key), check=False)
        if p.returncode != 0 and origin:
            # The bundle builds on commits this repository lacks; get them from origin, then retry.
            git(base, "fetch", "-q", "origin", env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), check=False)
            p = git(base, "fetch", "-q", bundle, "+%s:%s" % (key, key), check=False)
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace")
            if "prerequisite" in err or "lacks" in err or "does not have" in err:
                emit({"error": "prerequisite", "detail": err.strip()[-300:]})
                return 3
            emit({"error": "bundle", "detail": err.strip()[-500:]})
            return 1
    if not has_commit(base, cwip):
        emit({"error": "missing"})
        return 4
    if not is_worktree(top):
        if os.path.exists(top) and os.listdir(top):
            emit({"error": "path", "detail": "%s exists in the sandbox and is not a git worktree" % top})
            return 1
        ensure_dir(os.path.dirname(top))
        git(base, "worktree", "add", "-q", "--detach", "--no-checkout", top, sha)
    elif force != "1":
        mark = read_marker(top)
        if mark:
            now = worktree_tree(top, mark.get("includes", []))
            if now != mark.get("tree") and worktree_tree(top, inc) != tree:
                emit({"sandbox_changed": name_status(top, mark["tree"], now)[:60]})
                return 5
    git(top, "checkout", "-q", "--detach", "--force", sha)
    git(top, "clean", "-q", "-fd", *[a for p in TLC_CLEAN_KEEP for a in ("-e", p)])
    git(top, "read-tree", "-u", "--reset", cwip)
    git(top, "reset", "-q")
    now = worktree_tree(top, inc)
    write_marker(top, sha, now, cwip, inc)
    git(top, "update-ref", key, cwip)
    emit({"ok": True, "tree_match": now == tree, "base": base})
    return 0


def op_patch(top):
    mark = read_marker(top) if is_worktree(top) else None
    if not mark:
        emit({"error": "no copy of this worktree in the sandbox yet"})
        return 1
    now = worktree_tree(top, mark.get("includes", []))
    files = name_status(top, mark["tree"], now) if now != mark["tree"] else []
    if files:
        sys.stdout.buffer.write(git(top, "diff", "--binary", "--full-index", "--no-renames",
                                    mark["tree"], now).stdout)
        sys.stdout.flush()
    emit({"files": files, "tree": now})
    return 0


def op_changes(top):
    mark = read_marker(top) if is_worktree(top) else None
    if not mark:
        emit({"files": []})
        return 0
    now = worktree_tree(top, mark.get("includes", []))
    emit({"files": name_status(top, mark["tree"], now) if now != mark["tree"] else []})
    return 0


def op_tar(base_dir, path):
    full = path if os.path.isabs(path) else os.path.join(base_dir, path)
    if not os.path.exists(full):
        emit({"error": "%s does not exist in the sandbox" % full})
        return 1
    out = "/tmp/fxc-pull.tgz"
    with tarfile.open(out, "w:gz", dereference=True) as tar:
        tar.add(full, arcname=os.path.basename(full.rstrip("/")))
    emit({"tarball": out, "bytes": os.path.getsize(out)})
    return 0


def op_packlogs(log_dir, out):
    picked, skipped = pick_logs(log_entries(log_dir), LOG_FILE_LIMIT_BYTES, LOG_TOTAL_LIMIT_BYTES)
    with tarfile.open(out, "w:gz") as tar:
        for relpath in picked:
            tar.add(os.path.join(log_dir, relpath), arcname=relpath, recursive=False)
        if skipped:
            add_skipped_manifest(tar, skipped)
    emit({"packed": len(picked), "skipped": len(skipped), "tarball": out})
    return 0


def pick_logs(entries, file_limit_bytes, total_limit_bytes):
    picked = []
    skipped = []
    total_bytes = 0
    for relpath, size in sorted(entries):
        if not is_log_path(relpath):
            continue
        if size > file_limit_bytes or total_bytes + size > total_limit_bytes:
            skipped.append((relpath, size))
            continue
        picked.append(relpath)
        total_bytes += size
    return picked, skipped


def is_log_path(relpath):
    *folders, name = relpath.split("/")
    if any(folder.endswith(SKIPPED_DIR_SUFFIXES) for folder in folders):
        return False
    return name.endswith(LOG_SUFFIXES)


def log_entries(log_dir):
    entries = []
    for root, dirs, files in os.walk(log_dir, followlinks=False):
        dirs[:] = [d for d in dirs if not d.endswith(SKIPPED_DIR_SUFFIXES)]
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            relpath = os.path.relpath(path, log_dir).replace(os.sep, "/")
            entries.append((relpath, os.path.getsize(path)))
    return entries


def add_skipped_manifest(tar, skipped):
    entries = [{"path": relpath, "bytes": size} for relpath, size in skipped]
    data = json.dumps(entries).encode("utf-8")
    info = tarfile.TarInfo(SKIPPED_MANIFEST)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


# --------------------------------------------------------------------------- plain folder copies

PLAIN_STATE = os.path.expanduser("~/.cache/fx-cloud/plain")


def plain_state_path(root):
    return os.path.join(PLAIN_STATE, ref_key(root) + ".json")


def read_plain(root):
    try:
        with open(plain_state_path(root)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def op_runplain(root, cwd, digest, pidfile, script):
    state = read_plain(root)
    if not os.path.isdir(root) or state.get("digest") != digest:
        emit({"needs_push": True, "remote_digest": state.get("digest") if os.path.isdir(root) else None})
        return NEEDS_PUSH
    return spawn(cwd if os.path.isdir(cwd) else root, pidfile, script)


def op_applyplain(root, digest, base, tar_arg, deletes):
    """One-way copy of a folder that is not a git worktree. BASE is the digest the delta builds on,
    or "-" for a full copy."""
    state = read_plain(root)
    if base != "-" and (state.get("digest") != base or not os.path.isdir(root)):
        emit({"needs_push": True, "needs_full": True})
        return NEEDS_PUSH
    ensure_dir(root)
    real_root = os.path.realpath(root)
    tar_path = inline_file(tar_arg, "plain")
    try:
        if tar_path != "-":
            with tarfile.open(tar_path, "r:gz") as tar:
                for m in tar.getmembers():
                    target = os.path.realpath(os.path.join(real_root, m.name))
                    if not target.startswith(real_root + os.sep):
                        emit({"error": "path", "detail": "refusing %r" % m.name})
                        return 1
                    if m.isdir():
                        os.makedirs(target, exist_ok=True)
                    elif m.isreg():
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        if os.path.islink(target):
                            os.unlink(target)
                        with tar.extractfile(m) as src, open(target, "wb") as out:
                            shutil.copyfileobj(src, out)
                        os.chmod(target, (m.mode & 0o755) | 0o600)
        for rel in decode_list(deletes):
            target = os.path.realpath(os.path.join(real_root, rel))
            if target.startswith(real_root + os.sep) and (os.path.isfile(target) or os.path.islink(target)):
                os.unlink(target)
    finally:
        drop_inline(tar_path)
    os.makedirs(PLAIN_STATE, exist_ok=True)
    with open(plain_state_path(root) + ".tmp", "w") as f:
        json.dump({"digest": digest, "root": root}, f)
    os.replace(plain_state_path(root) + ".tmp", plain_state_path(root))
    emit({"ok": True})
    return 0


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    op, rest = argv[0], argv[1:]
    if op == "repo" and len(rest) == 1:
        print(repo_for(https_url(rest[0])))
        return 0
    ops = {"run": (op_run, 8), "apply": (op_apply, 9), "patch": (op_patch, 1),
           "changes": (op_changes, 1), "tar": (op_tar, 2), "packlogs": (op_packlogs, 2),
           "runplain": (op_runplain, 5), "applyplain": (op_applyplain, 5)}
    if op not in ops or not rest or rest[0] != PROTOCOL:
        # A newer Mac script may send an op that this helper lacks; both cases need the new helper.
        emit({"error": "old_helper", "have": PROTOCOL})
        return OLD_HELPER
    fn, nargs = ops[op]
    args = rest[1:]
    if len(args) != nargs:
        emit({"error": "usage", "detail": "%s takes %d arguments" % (op, nargs)})
        return 2
    try:
        return fn(*args)
    except GitError as e:
        emit({"error": "git", "detail": str(e)})
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
