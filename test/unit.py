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
import shutil
import subprocess
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
