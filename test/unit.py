#!/usr/bin/env python3
"""Unit tests for fx-cloud. Standard library only: no network, no sandboxes, no Vercel account.

    python3 test/unit.py
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import unittest
import zlib
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


fxc = load("fxcloud", os.path.join(ROOT, "fx-cloud"))
remote = load("fxcremote", os.path.join(ROOT, "remote", "fxc-remote.py"))
tla = load("fxctla", os.path.join(ROOT, "extras", "fx", "tla.py"))

CLEAR_VARS = list(fxc.ENV_KEYS) + ["FXC_CONFIG_DIR", "FXC_STATE_DIR", "FXC_OWNER", "FXC_LOCAL", "FXC_SHIM_DRYRUN"]

LIST_OUTPUT = """\
NAME                       STATUS    CREATED         REGION   MEMORY      VCPUS   IMAGE   TIMEOUT   SNAPSHOT   TAGS
fxt-0123456789ab           running   1 minute ago    iad1     16,384 MB   8       img     in 29m    snap_AAA   fxc:agent
fxt-abcdefabcdef           stopped   1 hour ago      iad1     16,384 MB   8       img     -         snap_OLD   fxc:agent
fxt-base-20261113-120000   stopped   2 hours ago     iad1     16,384 MB   8       img     -         -          fxc:base
fxt-probe-120000-1         running   1 minute ago    iad1     16,384 MB   8       img     in 29m    snap_AAA   fxc:probe
fxt-0123456789abc          running   1 minute ago    iad1     16,384 MB   8       img     in 29m    snap_AAA   -
fxa-0123456789ab           running   1 minute ago    iad1     16,384 MB   8       img     in 29m    snap_LIVE  fxc:agent
fxc-base-20261112-000000   stopped   1 day ago       iad1     16,384 MB   8       img     -         -          fxc:base

More results: sandbox sandbox list --all --cursor
"""


class Case(unittest.TestCase):
    """Each test gets its own settings and state folders, and the settings of team acme."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fxc-unit-")
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        for var in CLEAR_VARS:
            os.environ.pop(var, None)
        os.environ["FXC_CONFIG_DIR"] = os.path.join(self.tmp, "config")
        os.environ["FXC_STATE_DIR"] = os.path.join(self.tmp, "state")
        fxc.configure_paths()
        self.saved = (dict(fxc.CFG), fxc.CONFIG_FOUND, os.getcwd())
        self.use()

    def tearDown(self):
        os.chdir(self.saved[2])
        self.env.stop()
        fxc.configure_paths()
        fxc.CFG, fxc.CONFIG_FOUND = dict(self.saved[0]), self.saved[1]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use(self, **settings):
        fxc.CFG = fxc.check_config(dict({"team": "acme", "project": "builds", "prefix": "fxt-"}, **settings))

    def write_config(self, data):
        os.makedirs(fxc.CONFIG_DIR, exist_ok=True)
        with open(fxc.CONFIG_FILE, "w") as f:
            json.dump(data, f)

    def git_repo(self, name="proj", origin=None):
        path = os.path.realpath(os.path.join(self.tmp, name))
        os.makedirs(path)
        run = lambda *a: subprocess.run(["git", "-C", path] + list(a), check=True, capture_output=True)  # noqa: E731
        run("init", "-q", "-b", "main")
        run("config", "user.name", "Test")
        run("config", "user.email", "test@example.com")
        run("config", "commit.gpgsign", "false")
        with open(os.path.join(path, "file"), "w") as f:
            f.write("one\n")
        run("add", "file")
        run("commit", "-q", "-m", "first")
        if origin:
            run("remote", "add", "origin", origin)
        return path

    def run_main(self, argv):
        """(exit code, stdout, stderr) of fx-cloud's main(ARGV), with a Vercel CLI in PATH."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(fxc.shutil, "which", return_value="/usr/bin/vercel"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = fxc.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class ConfigTests(Case):
    def test_defaults_from_a_minimal_file(self):
        self.write_config({"team": "acme", "project": "builds"})
        cfg, found = fxc.load_config()
        self.assertTrue(found)
        self.assertEqual((cfg["team"], cfg["project"], cfg["prefix"]), ("acme", "builds", "fxc-"))
        self.assertEqual((cfg["vcpus"], cfg["idle_minutes"], cfg["max_agents"]), (8, 30, 24))
        self.assertEqual((cfg["local_only"], cfg["warm"], cfg["copy_skills"]), ([], [], True))
        self.assertEqual((cfg["agent_command"], cfg["setup_hook"]), (["fx"], None))

    def test_no_default_team_or_project(self):
        self.assertIsNone(fxc.DEFAULTS["team"])
        self.assertIsNone(fxc.DEFAULTS["project"])
        self.write_config({"project": "builds"})
        with self.assertRaises(fxc.ConfigError) as ctx:
            fxc.load_config()
        self.assertIn("team is not set", str(ctx.exception))

    def test_environment_overrides_the_file(self):
        self.write_config({"team": "acme", "project": "builds", "vcpus": 4, "prefix": "abc-", "idle_minutes": 20})
        os.environ.update({"FXC_TEAM": "other", "FXC_PROJECT": "p2", "FXC_PREFIX": "fxt-", "FXC_VCPUS": "2",
                           "FXC_IDLE_MINUTES": "45", "FXC_MAX_AGENTS": "3"})
        cfg, _ = fxc.load_config()
        self.assertEqual((cfg["team"], cfg["project"], cfg["prefix"]), ("other", "p2", "fxt-"))
        self.assertEqual((cfg["vcpus"], cfg["idle_minutes"], cfg["max_agents"]), (2, 45, 3))

    def test_folders_come_from_the_environment(self):
        self.assertEqual(fxc.CONFIG_FILE, os.path.join(self.tmp, "config", "config.json"))
        self.assertEqual(fxc.AGENTS_DIR, os.path.join(self.tmp, "state", "agents"))
        self.assertTrue(fxc.PUSH_CACHE.startswith(os.path.join(self.tmp, "state")))

    def test_missing_config_points_to_init(self):
        with self.assertRaises(fxc.ConfigError) as ctx:
            fxc.load_config()
        self.assertIn("fx-cloud init", str(ctx.exception))
        os.environ.update({"FXC_TEAM": "acme", "FXC_PROJECT": "builds"})  # variables alone are not a setup
        with self.assertRaises(fxc.ConfigError):
            fxc.load_config()

    def test_every_command_but_init_stops_without_config(self):
        for argv in (["run", "true"], ["push"], ["pull", "x"], ["ls"], ["gc"], ["done"], ["status"],
                     ["base"], ["setup"], ["login"], ["shell"], ["extend", "1h"], ["agent", "x"],
                     ["macos-build"], ["stop"]):
            code, _, err = self.run_main(argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("run `fx-cloud init`", err, argv)

    def test_bad_values_are_errors(self):
        for bad in ({"vcpus": "many"}, {"vcpus": True}, {"prefix": "Bad_"}, {"prefix": ""},
                    {"idle_minutes": 2}, {"max_agents": 0}, {"warm": [{"run": ["zig build"]}]},
                    {"warm": {"repo": "x"}}, {"local_only": "fxd"}, {"agent_command": []},
                    {"copy_skills": "yes"}, {"setup_hook": 3}, {"team": 5}):
            with self.assertRaises(fxc.ConfigError, msg=str(bad)):
                fxc.check_config(dict({"team": "acme", "project": "builds"}, **bad))

    def test_normalized_values(self):
        cfg = fxc.check_config({"team": " acme ", "project": "builds", "agent_command": "wrap fx",
                                "setup_hook": "~/hook.sh", "vcpus": "4",
                                "warm": [{"repo": " https://example.com/a.git ", "run": ["zig build"]}]})
        self.assertEqual(cfg["team"], "acme")
        self.assertEqual(cfg["agent_command"], ["wrap", "fx"])
        self.assertEqual(cfg["setup_hook"], os.path.join(os.path.expanduser("~"), "hook.sh"))
        self.assertEqual(cfg["vcpus"], 4)
        self.assertEqual(cfg["warm"], [{"repo": "https://example.com/a.git", "run": ["zig build"]}])

    def test_unknown_settings_get_a_note(self):
        self.write_config({"team": "acme", "project": "builds", "teams": "typo"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            fxc.load_config()
        self.assertIn("teams", err.getvalue())

    def test_base_file_belongs_to_its_settings(self):
        fxc.write_base({"snapshot": "snap_X", "sandbox": "fxt-base-1", "team": "acme", "project": "builds",
                        "prefix": "fxt-", "created": 0})
        self.assertEqual(fxc.read_base()["snapshot"], "snap_X")
        self.use(team="other")
        self.assertIsNone(fxc.read_base())
        self.use(prefix="fxq-")
        self.assertIsNone(fxc.read_base())


class RoutingTests(Case):
    def test_zig_routes(self):
        cases = {
            ("build",): "remote-install", ("build", "install"): "remote-install",
            ("build", "-Doptimize=ReleaseFast"): "remote-install", ("build", "test"): "remote-run",
            ("build", "test", "--summary", "all"): "remote-run", ("build", "run"): "local",
            ("build", "run", "--", "test"): "local", ("build", "--help"): "local", ("build", "-l"): "local",
            ("build", "--build-runner", "x"): "local", ("test", "src/main.zig"): "remote-run",
            ("version",): "local", ("fmt", "."): "local", ("cc", "x.c"): "local", (): "local",
        }
        for args, want in cases.items():
            self.assertEqual(fxc.zig_route(list(args)), want, args)

    def test_zig_build_parts(self):
        self.assertEqual(fxc.zig_build_parts(["test", "--prefix", "out", "-Dtarget=x86_64-linux", "-j", "4"]),
                         (["test"], "out", True))
        self.assertEqual(fxc.zig_build_parts(["--prefix=o", "install", "--", "run"]), (["install"], "o", False))

    def test_bun_routes(self):
        os.chdir(self.tmp)
        os.makedirs("tests/e2e")
        open("tests/e2e/driver.ts", "w").close()
        open("script.ts", "w").close()
        self.assertEqual(fxc.bun_route(["test"]), "remote-run")
        self.assertEqual(fxc.bun_route(["tests/e2e/driver.ts"]), "remote-run")
        self.assertEqual(fxc.bun_route(["./tests/e2e/driver.ts"]), "remote-run")
        self.assertEqual(fxc.bun_route(["script.ts"]), "local")
        self.assertEqual(fxc.bun_route(["install"]), "local")
        os.chdir("tests/e2e")
        self.assertEqual(fxc.bun_route(["driver.ts"]), "remote-run")

    def test_local_only_by_origin_and_by_folder(self):
        app = self.git_repo("mac-app-task-1", origin="git@github.com:acme/mac-app.git")
        tools = self.git_repo("native-tools", origin="https://example.com/x/other.git")
        plain = self.git_repo("server", origin="https://github.com/acme/server.git")
        self.use(local_only=[])
        self.assertTrue(fxc.project_allows_remote(app))
        self.use(local_only=["mac-app", "native-tools"])
        self.assertFalse(fxc.project_allows_remote(app))  # origin URL
        self.assertFalse(fxc.project_allows_remote(tools))  # a folder name in the path
        self.assertTrue(fxc.project_allows_remote(plain))
        self.assertFalse(fxc.project_allows_remote(os.path.expanduser("~")))

    def test_shim_dry_run_respects_local_only(self):
        app = self.git_repo("mac-app", origin="git@github.com:acme/mac-app.git")
        os.chdir(app)
        os.environ["FXC_SHIM_DRYRUN"] = "1"
        code, out, _ = self.run_main(["zig", "--real", sys.executable, "--", "build", "test"])
        self.assertEqual((code, out.strip()), (0, "zig build test -> remote-run"))
        self.write_config({"team": "acme", "project": "builds", "local_only": ["mac-app"]})
        code, out, _ = self.run_main(["zig", "--real", sys.executable, "--", "build", "test"])
        self.assertEqual((code, out.strip()), (0, "zig build test -> local"))

    def test_shims_need_settings_only_to_send_work_away(self):
        app = self.git_repo("server")
        os.chdir(app)
        os.environ["FXC_SHIM_DRYRUN"] = "1"
        code, out, _ = self.run_main(["bun", "--real", sys.executable, "--", "test"])
        self.assertEqual((code, out.strip()), (0, "bun test -> remote-run"))
        del os.environ["FXC_SHIM_DRYRUN"]
        code, _, err = self.run_main(["zig", "--real", sys.executable, "--", "build", "test"])
        self.assertEqual(code, 1)
        self.assertIn("fx-cloud init", err)
        self.assertIn("FXC_LOCAL=1", err)

    def test_tla_routes(self):
        repo = self.git_repo("fx")
        os.makedirs(os.path.join(repo, "tests/tla/spec"))
        for path in ("tests/tla/spec/check.sh", "tests/tla/run.py", "build.sh"):
            open(os.path.join(repo, path), "w").close()
        os.chdir(repo)
        self.assertEqual(tla.tla_route(fxc, "sh", ["tests/tla/spec/check.sh"]), "remote-run")
        self.assertEqual(tla.tla_route(fxc, "bash", ["-e", "tests/tla/spec/check.sh"]), "remote-run")
        self.assertEqual(tla.tla_route(fxc, "python3", ["tests/tla/run.py", "--all"]), "remote-run")
        self.assertEqual(tla.tla_route(fxc, "sh", ["build.sh"]), "local")
        self.assertEqual(tla.tla_route(fxc, "sh", ["-c", "tests/tla/spec/check.sh"]), "local")
        os.chdir("tests/tla/spec")
        self.assertEqual(tla.tla_route(fxc, "sh", ["./check.sh"]), "remote-run")


class NamingTests(Case):
    def test_agent_names(self):
        a = fxc.agent_name("fx:1@x", "/w/a")
        self.assertRegex(a, r"^fxt-[0-9a-f]{12}$")
        self.assertEqual(a, fxc.agent_name("fx:1@x", "/w/a"))
        self.assertNotEqual(a, fxc.agent_name("fx:2@x", "/w/a"))
        self.assertNotEqual(a, fxc.agent_name("fx:1@x", "/w/b"))
        self.use(team="other")
        self.assertNotEqual(a, fxc.agent_name("fx:1@x", "/w/a"))

    def test_is_agent_name_only_matches_this_prefix(self):
        self.assertTrue(fxc.is_agent_name("fxt-0123456789ab"))
        for name in ("fxt-base-20261113-120000", "fxt-probe-1-0", "fxt-0123456789abc", "fxt-0123456789aG",
                     "fxa-0123456789ab", "fxc-base-1", "dev-box", "review-1", "xfxt-0123456789ab"):
            self.assertFalse(fxc.is_agent_name(name), name)
        self.use(prefix="fx")
        self.assertFalse(fxc.is_agent_name("fxt-0123456789ab"))  # the rest is not 12 hex digits
        self.assertFalse(fxc.is_agent_name("fxc-0123456789ab"))

    def test_build_out(self):
        self.assertEqual(fxc.build_out("aarch64-macos"), "zig-out/.fx-cloud-aarch64-macos")
        self.assertEqual(fxc.build_out("x86_64-linux-gnu.2.31"), "zig-out/.fx-cloud-x86_64-linux-gnu.2.31")
        self.assertEqual(fxc.build_out("a/b c"), "zig-out/.fx-cloud-a_b_c")

    def test_agent_programs(self):
        self.assertEqual(fxc.agent_program("fx"), "fx")
        self.assertEqual(fxc.agent_program("/home/x/.local/bin/fx --resume 1"), "fx")
        self.assertEqual(fxc.agent_program("claude --model opus"), "claude")
        self.assertEqual(fxc.agent_program("node /opt/homebrew/bin/claude"), "claude")
        self.assertEqual(fxc.agent_program("/usr/local/lib/node_modules/codex-cli/vendor/codex/codex"), "codex")
        self.assertEqual(fxc.agent_program("node /usr/local/bin/codex.js resume 1"), "codex")
        self.assertIsNone(fxc.agent_program("fx __fx_foreground_session__ 3"))
        self.assertIsNone(fxc.agent_program("/bin/zsh -c 'fx-cloud run x'"))
        self.assertIsNone(fxc.agent_program("node server.js"))

    def test_owner_is_the_nearest_agent(self):
        procs = {10: (1, "a", "/sbin/launchd"), 20: (10, "b", "fx"), 30: (20, "c", "claude"),
                 40: (30, "d", "/bin/zsh -c make"), 50: (40, "e", "python3 fx-cloud run make")}
        owner = fxc.find_owner(procs, start_pid=50)
        self.assertEqual((owner["pid"], owner["prog"], owner["start"]), (30, "claude", "c"))
        self.assertIsNone(fxc.find_owner({50: (1, "e", "bash")}, start_pid=50))
        os.environ["FXC_OWNER"] = "script-1"
        self.assertEqual(fxc.current_owner(), ("named:script-1", None))


class PrefixFilterTests(Case):
    def fake_run(self, calls, stdout=LIST_OUTPUT):
        def run(argv, **_kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
        return run

    def write_entries(self):
        mine = {"name": "fxt-0123456789ab", "team": "acme", "project": "builds", "owner": "named:t",
                "root": self.tmp, "created": 0, "used": 0}
        others = [dict(mine, name="fxa-0123456789ab"),  # another prefix
                  dict(mine, name="fxt-ffffffffffff", team="other"),  # another team
                  dict(mine, name="fxt-base-1")]
        for e in [mine] + others:
            fxc.write_entry(e)
        return mine

    def test_parse_rows(self):
        rows = fxc.parse_sandbox_rows(LIST_OUTPUT)
        self.assertIn(("fxt-0123456789ab", "running", "snap_AAA"), rows)
        self.assertIn(("fxt-base-20261113-120000", "stopped", None), rows)
        self.assertFalse(any(r[0] in ("NAME", "More") for r in rows))

    def test_list_agents_keeps_only_this_prefix(self):
        calls = []
        with mock.patch.object(fxc.subprocess, "run", self.fake_run(calls)):
            agents = fxc.list_agents()
        self.assertEqual(agents, {"fxt-0123456789ab": ("running", "snap_AAA"),
                                  "fxt-abcdefabcdef": ("stopped", "snap_OLD")})
        self.assertEqual(calls[0][calls[0].index("--name-prefix") + 1], "fxt-")
        self.assertEqual(calls[0][calls[0].index("--scope") + 1], "acme")

    def test_all_entries_keeps_only_this_prefix_and_team(self):
        self.write_entries()
        self.assertEqual([e["name"] for e in fxc.all_entries()], ["fxt-0123456789ab"])

    def test_gc_all_considers_only_this_prefix(self):
        self.write_entries()
        out, calls = io.StringIO(), []
        with mock.patch.object(fxc.subprocess, "run", self.fake_run(calls)), \
                mock.patch.object(fxc, "ps_table", return_value={}), \
                mock.patch.object(fxc, "remove_sandboxes", side_effect=AssertionError("dry run removed")), \
                contextlib.redirect_stdout(out):
            doomed, _ = fxc.run_gc(everything=True, dry_run=True)
        self.assertEqual(sorted(doomed), ["fxt-0123456789ab", "fxt-abcdefabcdef"])
        self.assertNotIn("fxa-", out.getvalue())
        self.assertNotIn("base", out.getvalue())

    def test_gc_removes_stopped_and_orphaned_agents_only(self):
        mine = self.write_entries()
        fxc.write_entry(dict(mine, name="fxt-111111111111", root=os.path.join(self.tmp, "gone"), created=1e12))
        calls = []
        with mock.patch.object(fxc.subprocess, "run", self.fake_run(calls)), \
                mock.patch.object(fxc, "ps_table", return_value={}):
            doomed, _ = fxc.gc_plan(fxc.all_entries(), fxc.list_remote(), {})
        self.assertEqual(doomed, {"fxt-111111111111": "its folder is gone",
                                  "fxt-abcdefabcdef": "no agent on this Mac uses it"})

    def test_remove_sandboxes_never_touches_other_names(self):
        calls = []
        with mock.patch.object(fxc.subprocess, "run", self.fake_run(calls, stdout="")):
            fxc.remove_sandboxes(["fxa-0123456789ab", "fxt-base-20261113-120000", "dev-box", "fxt-0123456789ab"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], "fxt-0123456789ab")
        self.assertNotIn("fxa-0123456789ab", calls[0])
        self.assertEqual(fxc.remove_sandboxes(["fxa-0123456789ab", "fxc-base-1"]), [])

    def test_ls_lists_only_this_prefix(self):
        self.write_entries()
        with mock.patch.object(fxc.subprocess, "run", self.fake_run([])), \
                mock.patch.object(fxc, "ps_table", return_value={}):
            code, out, _ = self.run_main_ls()
        self.assertEqual(code, 0)
        self.assertIn("fxt-0123456789ab", out)
        self.assertIn("fxt-abcdefabcdef", out)
        for other in ("fxa-", "fxt-base", "fxc-base", "fxt-probe", "fxt-ffffffffffff"):
            self.assertNotIn(other, out)

    def run_main_ls(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fxc.cmd_ls([])
        return code, out.getvalue(), ""


class HelperTests(Case):
    def test_protocol_matches_the_sandbox_helper(self):
        self.assertEqual(fxc.PROTOCOL, remote.PROTOCOL)

    def test_https_url_and_repo_slug(self):
        self.assertEqual(remote.https_url("git@github.com:acme/app.git"), "https://github.com/acme/app.git")
        self.assertEqual(remote.https_url("https://github.com/acme/app"), "https://github.com/acme/app.git")
        self.assertEqual(remote.https_url("ssh://git@gitlab.com/a/b.git"), "https://gitlab.com/a/b.git")
        self.assertEqual(remote.https_url("https://example.com/a/b/"), "https://example.com/a/b")
        self.assertEqual(remote.repo_slug("https://github.com/acme/app.git"), "github.com__acme__app")
        self.assertEqual(remote.repo_slug(remote.https_url("git@github.com:acme/app.git")),
                         remote.repo_slug("https://github.com/acme/app"))

    def test_tlc_lists_match(self):
        self.assertEqual(fxc.TLC_PATHSPECS, remote.TLC_PATHSPECS)

    def test_rule_block_is_stripped(self):
        text = "# Rules\n\nkeep\n\n%s\n## Heavy\nrun it\n%s\n\nafter\n" % (fxc.RULE_START, fxc.RULE_END)
        self.assertEqual(fxc.strip_rule_block(text), "# Rules\n\nkeep\n\nafter\n")
        self.assertEqual(fxc.strip_rule_block("%s\nx\n%s\n" % (fxc.RULE_START, fxc.RULE_END)), "")
        self.assertEqual(fxc.strip_rule_block("plain\n"), "plain\n")

    def test_install_uses_the_same_markers(self):
        for script in ("install.sh", "uninstall.sh"):
            with open(os.path.join(ROOT, script)) as f:
                text = f.read()
            self.assertIn("RULE_START='%s'" % fxc.RULE_START, text, script)
            self.assertIn("RULE_END='%s'" % fxc.RULE_END, text, script)

    def test_take_flags(self):
        flags, rest = fxc.take_flags(["--force", "--include", "a", "--include", "b", "zig", "--force"],
                                     ("--force",), multi=("--include",))
        self.assertEqual(flags, {"--force": True, "--include": ["a", "b"]})
        self.assertEqual(rest, ["zig", "--force"])
        self.assertEqual(fxc.take_flags(["--", "--force"], ("--force",)), ({}, ["--force"]))

    def test_real_tool_skips_shims(self):
        shim_dir, real_dir = os.path.join(self.tmp, "shims"), os.path.join(self.tmp, "real")
        os.makedirs(shim_dir)
        os.makedirs(real_dir)
        shutil.copy(os.path.join(ROOT, "shims", "zig"), os.path.join(shim_dir, "zig"))
        with open(os.path.join(real_dir, "zig"), "w") as f:
            f.write("#!/bin/sh\necho 0.0.1\n")
        os.chmod(os.path.join(real_dir, "zig"), 0o755)
        os.environ["PATH"] = shim_dir + os.pathsep + real_dir
        self.assertEqual(fxc.real_tool("zig"), os.path.join(real_dir, "zig"))

    def test_local_tree_sees_uncommitted_files_and_is_stable(self):
        repo = self.git_repo("sync")
        clean = fxc.local_state(repo)
        with open(os.path.join(repo, "file"), "w") as f:
            f.write("two\n")
        changed = fxc.local_state(repo)
        self.assertEqual(clean["sha"], changed["sha"])
        self.assertNotEqual(clean["tree"], changed["tree"])
        self.assertEqual(fxc.copy_info(dict(changed))["cwip"], fxc.copy_info(dict(fxc.local_state(repo)))["cwip"])


class TlaLogTests(unittest.TestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="fxc-tla-unit-")
        self.addCleanup(folder.cleanup)
        self.tmp = folder.name
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FXC_SHIM_DRYRUN", None)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def write_text(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def add_file(self, tar, name, data):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    def test_run_dir_is_per_user_and_per_run(self):
        self.assertEqual(tla.run_dir_path(501, "ab12cd34"), "/tmp/fxc-tla-501/ab12cd34")

    def test_remote_script_gives_the_command_its_own_tmpdir_and_keeps_its_exit_code(self):
        run_dir, seen, packed = self.path("run"), self.path("seen"), self.path("packed")
        command = "sh -c " + shlex.quote('printf hi > "$TMPDIR/a.log"; printf %s "$TMPDIR" > '
                                         + shlex.quote(seen) + "; exit 7")
        pack_command = 'cat "$TMPDIR/a.log" > ' + shlex.quote(packed)
        script = tla.remote_script(command, run_dir, pack_command)
        result = subprocess.run(["bash", "-c", script], capture_output=True)
        self.assertEqual(result.returncode, 7)
        with open(seen) as f:
            self.assertEqual(f.read(), run_dir)
        with open(packed) as f:
            self.assertEqual(f.read(), "hi")
        self.assertTrue(os.path.isfile(os.path.join(run_dir, "a.log")))

    def test_remote_script_warns_and_still_runs_when_the_run_dir_cannot_be_made(self):
        blocker, seen = self.path("file"), self.path("seen")
        self.write_text(blocker, "not a folder\n")
        run_dir = os.path.join(blocker, "run")
        command = "sh -c " + shlex.quote("echo ran > " + shlex.quote(seen) + "; exit 7")
        script = tla.remote_script(command, run_dir, "true")
        result = subprocess.run(["bash", "-c", script], capture_output=True)
        self.assertEqual(result.returncode, 7)
        with open(seen) as f:
            self.assertEqual(f.read(), "ran\n")
        warning = "fx-cloud: could not make %s, so the logs of this run stay in the sandbox\n" % run_dir
        self.assertIn(warning, result.stderr.decode())

    def test_pick_logs_keeps_small_logs_and_reports_big_ones(self):
        entries = [("b/x.log", 5), ("a/y.out", 3), ("a/z.txt", 1),
                   ("a/M.states/s.log", 1), ("a/M.tmp/t.log", 1), ("big.log", 50)]
        picked, skipped = remote.pick_logs(entries, 10, 100)
        self.assertEqual(picked, ["a/y.out", "b/x.log"])
        self.assertEqual(skipped, [("big.log", 50)])

    def test_pick_logs_stops_at_the_total_limit(self):
        picked, skipped = remote.pick_logs([("a.log", 6), ("b.log", 6)], 10, 10)
        self.assertEqual(picked, ["a.log"])
        self.assertEqual(skipped, [("b.log", 6)])

    def test_pick_logs_keeps_driver_reports_but_not_other_json(self):
        picked, skipped = remote.pick_logs([("a/report.json", 10), ("a/binary-report.json", 5),
                                            ("a/graph.json", 7)], 100, 100)
        self.assertEqual(picked, ["a/binary-report.json", "a/report.json"])
        self.assertEqual(skipped, [])

    def test_packlogs_packs_only_log_files_and_lists_skipped_ones(self):
        log_dir, out, outside = self.path("logs"), self.path("out.tgz"), self.path("outside.txt")
        self.write_text(os.path.join(log_dir, "a", "x.log"), "abc")
        self.write_text(os.path.join(log_dir, "a", "y.txt"), "no")
        self.write_text(os.path.join(log_dir, "a", "S.states", "z.log"), "zz")
        self.write_text(os.path.join(log_dir, "a", "T.tmp", "w.log"), "ww")
        self.write_text(os.path.join(log_dir, "big.log"), "x" * 20)
        self.write_text(outside, "secret")
        os.symlink(outside, os.path.join(log_dir, "link.log"))
        with mock.patch.object(remote, "LOG_FILE_LIMIT_BYTES", 10):
            code = remote.op_packlogs(log_dir, out)
        self.assertEqual(code, 0)
        with tarfile.open(out, "r:gz") as tar:
            names = sorted(tar.getnames())
            manifest = tar.extractfile(".fxc-skipped").read().decode()
        self.assertEqual(names, [".fxc-skipped", "a/x.log"])
        self.assertEqual(json.loads(manifest), [{"path": "big.log", "bytes": 20}])

    def test_packlogs_writes_an_empty_tarball_when_the_folder_is_missing(self):
        out = self.path("out.tgz")
        code = remote.op_packlogs(self.path("missing"), out)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(out))
        with tarfile.open(out, "r:gz") as tar:
            self.assertEqual(tar.getmembers(), [])

    def test_log_entries_does_not_walk_into_state_folders(self):
        log_dir = self.path("logs")
        self.write_text(os.path.join(log_dir, "a", "x.log"), "abc")
        self.write_text(os.path.join(log_dir, "a", "M.states", "deep", "s.log"), "state")
        real_getsize = os.path.getsize
        stated = []

        def record_getsize(path):
            stated.append(path)
            return real_getsize(path)

        with mock.patch.object(remote.os.path, "getsize", side_effect=record_getsize):
            entries = remote.log_entries(log_dir)
        self.assertEqual(entries, [("a/x.log", 3)])
        self.assertFalse([path for path in stated if ".states" in path])

    def test_dispatch_knows_packlogs(self):
        log_dir, out = self.path("logs"), self.path("out.tgz")
        os.makedirs(log_dir)
        with contextlib.redirect_stderr(io.StringIO()):
            ok = remote.main(["packlogs", remote.PROTOCOL, log_dir, out])
            usage = remote.main(["packlogs", remote.PROTOCOL, log_dir])
        self.assertEqual(ok, 0)
        self.assertEqual(usage, 2)

    def test_extract_logs_writes_only_files_inside_the_run_dir(self):
        tgz, dest = self.path("logs.tgz"), self.path("run")
        os.makedirs(dest)
        with tarfile.open(tgz, "w:gz") as tar:
            self.add_file(tar, "ok/x.log", b"hi")
            self.add_file(tar, "../evil.log", b"evil")
            link = tarfile.TarInfo("ok/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
            self.add_file(tar, ".fxc-skipped", json.dumps([{"path": "big.log", "bytes": 99}]).encode())
        result = tla.extract_logs(tgz, dest)
        self.assertEqual(result, (1, [("big.log", 99)], 2, None))
        with open(os.path.join(dest, "ok", "x.log")) as f:
            self.assertEqual(f.read(), "hi")
        self.assertEqual(os.stat(os.path.join(dest, "ok", "x.log")).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(self.path("evil.log")))
        self.assertFalse(os.path.lexists(os.path.join(dest, "ok", "link")))
        self.assertFalse(os.path.exists(os.path.join(dest, ".fxc-skipped")))

    def test_extract_logs_refuses_files_over_the_size_limits(self):
        tgz, dest = self.path("logs.tgz"), self.path("run")
        with tarfile.open(tgz, "w:gz") as tar:
            self.add_file(tar, "a.log", b"x" * 8)
            self.add_file(tar, "b.log", b"x" * 8)
            self.add_file(tar, "c.log", b"x" * 12)
        with mock.patch.object(tla, "MAX_LOG_FILE_BYTES", 10), mock.patch.object(tla, "MAX_LOG_TOTAL_BYTES", 15):
            result = tla.extract_logs(tgz, dest)
        self.assertEqual(result, (1, [], 2, None))
        self.assertEqual(os.listdir(dest), ["a.log"])

    def test_extract_logs_reports_a_broken_tarball(self):
        tgz = self.path("broken.tgz")
        with open(tgz, "wb") as f:
            f.write(b"this is not a tarball")
        count, skipped, refused, error = tla.extract_logs(tgz, self.path("run"))
        self.assertEqual(count, 0)
        self.assertEqual(skipped, [])
        self.assertEqual(refused, 0)
        self.assertIsInstance(error, str)
        self.assertTrue(error)

    def test_extract_logs_reports_zlib_errors(self):
        with mock.patch.object(tla.tarfile, "open", side_effect=zlib.error("bad stream")):
            result = tla.extract_logs(self.path("logs.tgz"), self.path("run"))
        self.assertEqual(result, (0, [], 0, "bad stream"))

    def test_extract_logs_escapes_control_characters_in_its_error(self):
        with mock.patch.object(tla.tarfile, "open", side_effect=tarfile.ReadError("bad \x1b[31mname")):
            result = tla.extract_logs(self.path("logs.tgz"), self.path("run"))
        self.assertEqual(result, (0, [], 0, "bad \\x1b[31mname"))

    def test_skipped_list_survives_tabs_and_newlines_in_names(self):
        log_dir, out, dest = self.path("logs"), self.path("out.tgz"), self.path("run")
        self.write_text(os.path.join(log_dir, "odd\tname\nx.log"), "x" * 20)
        with mock.patch.object(remote, "LOG_FILE_LIMIT_BYTES", 10):
            remote.op_packlogs(log_dir, out)
        self.assertEqual(tla.extract_logs(out, dest), (0, [("odd\tname\nx.log", 20)], 0, None))

    def test_skipped_note_escapes_control_characters_and_quotes_the_path(self):
        note = tla.skipped_note(("a\x1b[31mb.log", 31457280), "/tmp/fxc-tla-501/ab12cd34")
        self.assertEqual(note, "/tmp/fxc-tla-501/ab12cd34/a\\x1b[31mb.log is too large to copy back (30.0 MB); "
                               "get it with: fx-cloud pull $'/tmp/fxc-tla-501/ab12cd34/a\\x1b[31mb.log' <dest>")
        self.assertNotIn("\x1b", note)

    def test_skipped_note_shows_the_real_size_of_a_small_file(self):
        self.assertIn("(6 KB)", tla.skipped_note(("b.log", 6 * 1024), "/r"))
        self.assertIn("(300 bytes)", tla.skipped_note(("c.log", 300), "/r"))
        self.assertIn("(1023 KB)", tla.skipped_note(("d.log", 1048575), "/r"))

    def test_log_notes_mentions_refused_entries(self):
        self.assertEqual(tla.log_notes(0, [], 2, "/r"),
                         ["left out 2 entries from the sandbox that were not plain log files under /r"])

    def test_shell_quote_printable_round_trips_through_bash(self):
        texts = ["a'b\\c\x1bd é", "zw\u200bsp.log"]
        for shell in ("/bin/bash", "/bin/zsh"):
            for text in texts:
                with self.subTest(shell=shell, text=text):
                    if not os.path.exists(shell):
                        self.skipTest(shell + " is not installed")
                    result = subprocess.run([shell, "-f", "-c", "printf %s " + tla.shell_quote_printable(text)],
                                            capture_output=True, check=True)
                    self.assertEqual(result.stdout.decode("utf-8"), text)

    def test_read_manifest_keeps_only_valid_entries(self):
        manifest = [{"path": "a.log", "bytes": 5}, "x", {"path": "b.log"},
                    {"path": "c.log", "bytes": 1.5}, {"path": "d.log", "bytes": True}]
        self.assertEqual(tla.read_manifest(io.BytesIO(json.dumps(manifest).encode())), [("a.log", 5)])

    def test_read_manifest_ignores_invalid_json(self):
        self.assertEqual(tla.read_manifest(io.BytesIO(b"not json")), [])

    def test_logs_parent_must_be_a_private_folder_of_this_user(self):
        logs, link = self.path("logs"), self.path("link")
        self.assertTrue(tla.prepare_logs_parent(logs, os.getuid()))
        self.assertEqual(os.stat(logs).st_mode & 0o777, 0o700)
        os.symlink(logs, link)
        self.assertFalse(tla.prepare_logs_parent(link, os.getuid()))

    def test_logs_parent_of_another_user_is_refused(self):
        self.assertFalse(tla.prepare_logs_parent(self.path("logs"), os.getuid() + 1))

    def test_skipped_manifest_names_match(self):
        self.assertEqual(tla.SKIPPED_MANIFEST, remote.SKIPPED_MANIFEST)

    def test_mac_size_limits_match_the_sandbox_side(self):
        self.assertEqual(tla.MAX_LOG_FILE_BYTES, remote.LOG_FILE_LIMIT_BYTES)
        self.assertEqual(tla.MAX_LOG_TOTAL_BYTES, remote.LOG_TOTAL_LIMIT_BYTES)

    def test_main_runs_the_wrapped_command_and_brings_logs_back(self):
        repo = self.path("proj")
        self.write_text(os.path.join(repo, "tests", "tla", "demo", "check.sh"), "exit 0\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(repo)
        scripts = []

        def run_here(script, flags, capture=False):
            scripts.append(script)
            return 3, b""

        core = types.SimpleNamespace(
            take_flags=fxc.take_flags,
            die=fxc.die,
            subprocess=subprocess,
            project_allows_remote=lambda path: True,
            need_config=lambda tool: None,
            say=lambda msg: None,
            helper_script=lambda op, *a: "PACK " + op + " " + " ".join(a),
            run_here=run_here,
        )
        with mock.patch.object(tla, "bring_logs_back") as bring_back:
            code = tla.main(core, ["--tool", "sh", "--real", "/bin/sh", "--", "tests/tla/demo/check.sh"])
        self.assertEqual(code, 3)
        self.assertEqual(len(scripts), 1)
        self.assertIn("TMPDIR", scripts[0])
        self.assertIn("check.sh", scripts[0])
        self.assertIn("PACK packlogs", scripts[0])
        bring_back.assert_called_once()
        core_arg, run_dir, uid = bring_back.call_args.args
        self.assertIs(core_arg, core)
        self.assertEqual(uid, os.getuid())
        self.assertRegex(run_dir, "^/tmp/fxc-tla-%d/[0-9a-f]{8}$" % os.getuid())

    def test_main_keeps_the_exit_code_when_the_copy_back_breaks(self):
        repo = self.path("proj")
        self.write_text(os.path.join(repo, "tests", "tla", "demo", "check.sh"), "exit 0\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(repo)
        said = []
        core = types.SimpleNamespace(
            take_flags=fxc.take_flags,
            die=fxc.die,
            subprocess=subprocess,
            project_allows_remote=lambda path: True,
            need_config=lambda tool: None,
            say=said.append,
            helper_script=lambda op, *a: "PACK " + op + " " + " ".join(a),
            run_here=lambda script, flags, capture=False: (3, b""),
        )
        with mock.patch.object(tla, "bring_logs_back", side_effect=zlib.error("bad data")):
            code = tla.main(core, ["--tool", "sh", "--real", "/bin/sh", "--", "tests/tla/demo/check.sh"])
        self.assertEqual(code, 3)
        self.assertEqual(said[-1], "could not copy the TLC logs back: bad data")

    def copy_download(self, source, argv):
        with open(source, "rb") as f, open(argv[-1], "wb") as out:
            out.write(f.read())
        return types.SimpleNamespace(returncode=0, stderr="")

    def leftover_downloads(self):
        root = self.path("logs-root")
        return [name for name in os.listdir(root) if name.startswith("logs-") and name.endswith(".tgz")]

    def bring_back(self, run, run_dir):
        said = []
        core = types.SimpleNamespace(
            SANDBOX="fxa-test",
            say=said.append,
            vercel=lambda sub, *args: [sub] + list(args),
            subprocess=types.SimpleNamespace(run=run, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE),
        )
        tla.bring_logs_back(core, run_dir, os.getuid())
        return said

    def test_bring_logs_back_unpacks_the_download_and_says_what_it_copied(self):
        run_dir = self.path("logs-root", "ab12cd34")
        source = self.path("source.tgz")
        with tarfile.open(source, "w:gz") as tar:
            self.add_file(tar, "fx-tla-demo.X/Demo.log", b"model error")
            manifest = [{"path": "fx-tla-demo.X/Big.log", "bytes": 31457280}]
            self.add_file(tar, ".fxc-skipped", json.dumps(manifest).encode())
        call = mock.Mock(side_effect=lambda argv, **kw: self.copy_download(source, argv))
        said = self.bring_back(call, run_dir)
        with open(os.path.join(run_dir, "fx-tla-demo.X", "Demo.log")) as f:
            self.assertEqual(f.read(), "model error")
        call.assert_called_once()
        self.assertEqual(call.call_args.args[0][1], "fxa-test:" + run_dir + ".tgz")
        self.assertEqual(said, [
            "copied 1 TLC log file to " + run_dir,
            run_dir + "/fx-tla-demo.X/Big.log is too large to copy back (30.0 MB); get it with: fx-cloud pull "
            + shlex.quote(run_dir + "/fx-tla-demo.X/Big.log") + " <dest>",
        ])
        self.assertEqual(self.leftover_downloads(), [])

    def test_bring_logs_back_says_so_when_the_download_fails(self):
        run_dir = self.path("logs-root", "ab12cd34")
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=1, stderr="Vercel CLI 50.1\nError: file not found\n"))
        said = self.bring_back(run, run_dir)
        self.assertEqual(said, ["could not copy the TLC logs back from the sandbox: Error: file not found; "
                                "get them with: fx-cloud pull " + shlex.quote(run_dir + ".tgz") + " ."])
        self.assertEqual(self.leftover_downloads(), [])

    def test_bring_logs_back_keeps_going_when_the_download_file_cannot_be_made(self):
        run_dir = self.path("logs-root", "ab12cd34")
        call = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stderr=""))
        with mock.patch.object(tla.tempfile, "mkstemp", side_effect=OSError("disk full")):
            said = self.bring_back(call, run_dir)
        call.assert_not_called()
        self.assertEqual(said, ["could not copy the TLC logs back: disk full"])

    def test_fetch_logs_reports_why_the_cli_could_not_start(self):
        run = mock.Mock(side_effect=OSError("No such file or directory: 'vercel'"))
        core = types.SimpleNamespace(
            SANDBOX="fxa-test",
            vercel=lambda sub, *args: [sub] + list(args),
            subprocess=types.SimpleNamespace(run=run, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE),
        )
        self.assertEqual(tla.fetch_logs(core, "/r/x", "/tmp/t.tgz"), (False, "No such file or directory: 'vercel'"))

    def test_fetch_logs_names_the_error_when_it_has_no_text(self):
        run = mock.Mock(side_effect=OSError())
        core = types.SimpleNamespace(
            SANDBOX="fxa-test",
            vercel=lambda sub, *args: [sub] + list(args),
            subprocess=types.SimpleNamespace(run=run, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE),
        )
        self.assertEqual(tla.fetch_logs(core, "/r/x", "/tmp/t.tgz"), (False, "OSError"))

    def test_stale_downloads_picks_old_downloads_only(self):
        entries = [("logs-a.tgz", 0), ("logs-b.tgz", 9999), ("other.tgz", 0), ("logs-c.txt", 0)]
        self.assertEqual(tla.stale_downloads(entries, 10000), ["logs-a.tgz"])

    def test_bring_logs_back_removes_stale_downloads(self):
        folder = self.path("logs-root")
        hour_ago = time.time() - 3600
        old, new, other = [os.path.join(folder, name) for name in ("logs-old.tgz", "logs-new.tgz", "other.tgz")]
        for path in (old, other):
            self.write_text(path, "x")
            os.utime(path, (hour_ago, hour_ago))
        self.write_text(new, "x")
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=1, stderr="Error: gone\n"))
        self.bring_back(run, os.path.join(folder, "ab12cd34"))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertTrue(os.path.exists(other))


if __name__ == "__main__":
    unittest.main(verbosity=1)
