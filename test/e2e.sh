#!/bin/bash
# End-to-end test of fx-cloud.
#
#   FXC_E2E_TEAM=<team> FXC_E2E_PROJECT=<project> test/e2e.sh
#   test/e2e.sh --local      only the install.sh and uninstall.sh checks, without Vercel
#
# install.sh and uninstall.sh run with a throwaway home folder. The live part uses its own settings
# and state folders and the sandbox name prefix fxt-. It builds a base (base --no-warm, about 5
# minutes) and one agent sandbox in the given Vercel project, and removes every fxt- sandbox and
# the base snapshots at the end, also when a step fails. It does not start when sandboxes named
# fxt-* already exist there, so it never removes anything it did not make.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd -P)
FXC="$here/fx-cloud"
PREFIX=fxt-
live=1
[ "${1:-}" = "--local" ] && live=0

T=$(cd "$(mktemp -d /tmp/fx-cloud-e2e.XXXXXX)" && pwd -P)
passed=0
failed=0
live_started=0
ok() { passed=$((passed + 1)); echo "  ok    $1"; }
bad() { failed=$((failed + 1)); echo "  FAIL  $1"; }
check() { local what=$1; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }
contains() { grep -qF -- "$2" "$1"; }
count() { grep -cF -- "$2" "$1" 2>/dev/null || true; }
now() { date +%s; }

SCOPE=(--scope "${FXC_E2E_TEAM:-}" --project "${FXC_E2E_PROJECT:-}")
ours() {  # every sandbox in the project whose name starts with the test prefix
  vercel sandbox list --all "${SCOPE[@]}" --name-prefix "$PREFIX" 2>/dev/null \
    | awk -v p="$PREFIX" 'index($1, p) == 1 {print $1}'
}
snapshots_of() {  # snapshot IDs of sandbox $1 that are not deleted
  vercel sandbox snapshots list "${SCOPE[@]}" --name "$1" 2>/dev/null | awk '$1 ~ /^snap_/ && $2 != "deleted" {print $1}'
}

cleanup() {
  set +e
  if [ "$live_started" = 1 ]; then
    echo "cleanup:"
    names=$(ours)
    snaps=""
    for n in $names; do snaps="$snaps $(snapshots_of "$n")"; done
    if [ -f "$T/config/base.json" ]; then
      snaps="$snaps $(python3 -c 'import json, sys; b = json.load(open(sys.argv[1])); print(" ".join(b.get("snapshots", []) + b.get("retired", [])))' "$T/config/base.json")"
    fi
    if [ -f "$T/snapshots.txt" ]; then snaps="$snaps $(cat "$T/snapshots.txt")"; fi
    # shellcheck disable=SC2086 # split the list into words
    snaps=$(printf '%s\n' $snaps | sort -u)
    for n in $names; do
      case "$n" in "$PREFIX"*) vercel sandbox rm "${SCOPE[@]}" --delete-orphan-snapshots "$n" >/dev/null 2>&1 && echo "  removed $n" ;; esac
    done
    for s in $snaps; do
      vercel sandbox snapshots delete "${SCOPE[@]}" "$s" >/dev/null 2>&1 && echo "  deleted $s"
    done
    left=$(ours)
    echo "  sandboxes left with prefix $PREFIX: ${left:-none}"
    alive=""
    for s in $snaps; do
      status=$(vercel sandbox snapshots get "${SCOPE[@]}" "$s" 2>&1 | awk -v s="$s" '$1 == s {print $2} /[Nn]ot [Ff]ound|404/ {print "gone"}')
      case "$status" in deleted|gone) ;; *) alive="$alive $s" ;; esac
    done
    # shellcheck disable=SC2086
    echo "  snapshots of this run: $(echo $snaps | wc -w | tr -d ' '); not deleted: ${alive:-none}"
    if [ -n "$left" ] || [ -n "$alive" ]; then failed=$((failed + 1)); fi
  fi
  if [ "$failed" -gt 0 ] || [ "${KEEP:-0}" = 1 ]; then
    echo "Kept the test folder for a look: $T"
  else
    rm -rf "$T"
  fi
  echo "$passed passed, $failed failed"
  if [ "$failed" -gt 0 ]; then exit 1; fi
}
trap cleanup EXIT

# --------------------------------------------------------------------------- install.sh, uninstall.sh

echo "install.sh and uninstall.sh, in a throwaway home folder:"
H="$T/home"
mkdir -p "$H/.fx" "$H/.claude" "$H/.agents" "$H/.codex"
printf '# My rules\n\nKeep this.\n' >"$H/.fx/AGENTS.md"
[ "$H" != "$HOME" ] || { echo "the throwaway home is the real home; stopping"; exit 1; }
inst() { env -u FXC_CONFIG_DIR -u FXC_STATE_DIR HOME="$H" SHELL=/bin/zsh "$@"; }

check "install.sh --shims --add-rule succeeds" inst "$here/install.sh" --shims --add-rule
check "it links ~/.local/bin/fx-cloud to this repository" test "$(readlink "$H/.local/bin/fx-cloud")" = "$FXC"
for tool in fx claude agents; do
  check "it installs the skill in ~/.$tool/skills" cmp -s "$here/skill/SKILL.md" "$H/.$tool/skills/fx-cloud/SKILL.md"
done
check "it adds no skill to ~/.codex, which reads ~/.agents" test ! -e "$H/.codex/skills/fx-cloud"
for rc in .zprofile .zshrc; do
  check "it adds one shims line to ~/$rc" test "$(count "$H/$rc" "$here/shims:")" = 1
done
for f in .fx/AGENTS.md .claude/CLAUDE.md .codex/AGENTS.md; do
  check "it adds the rule to ~/$f" contains "$H/$f" "fx-cloud run <command>"
done
check "it keeps the existing rules" contains "$H/.fx/AGENTS.md" "Keep this."
cp -R "$H" "$T/home-after-one"
check "a second install succeeds" inst "$here/install.sh" --shims --add-rule
check "a second install changes nothing" diff -r "$T/home-after-one" "$H"
check "uninstall.sh succeeds" inst "$here/uninstall.sh"
check "it removes the link" test ! -e "$H/.local/bin/fx-cloud"
check "it removes the skills" test ! -e "$H/.fx/skills/fx-cloud" -a ! -e "$H/.claude/skills/fx-cloud" -a ! -e "$H/.agents/skills/fx-cloud"
check "it removes the shims lines" test "$(count "$H/.zprofile" "fx-cloud")$(count "$H/.zshrc" "fx-cloud")" = 00
check "it removes the rules and keeps the user's" test "$(count "$H/.fx/AGENTS.md" "fx-cloud")" = 0 -a "$(cat "$H/.fx/AGENTS.md")" = "$(printf '# My rules\n\nKeep this.')"
mkdir -p "$H/.config/fx-cloud" "$H/.cache/fx-cloud"
echo '{}' >"$H/.config/fx-cloud/base.json"
check "uninstall.sh --purge stops while a base snapshot is known" test "$(inst "$here/uninstall.sh" --purge >/dev/null 2>&1; echo $?)" = 1
check "and keeps the settings then" test -f "$H/.config/fx-cloud/base.json"
rm "$H/.config/fx-cloud/base.json"
echo '{}' >"$H/.config/fx-cloud/config.json"
check "uninstall.sh --purge succeeds" inst "$here/uninstall.sh" --purge
check "it removes the settings and state" test ! -e "$H/.config/fx-cloud" -a ! -e "$H/.cache/fx-cloud"

[ "$live" = 1 ] || exit 0

# --------------------------------------------------------------------------- a real sandbox

if [ -z "${FXC_E2E_TEAM:-}" ] || [ -z "${FXC_E2E_PROJECT:-}" ]; then
  bad "FXC_E2E_TEAM and FXC_E2E_PROJECT name the Vercel team and project for the live test (or use --local)"
  exit 1
fi
echo "live, in $FXC_E2E_TEAM/$FXC_E2E_PROJECT with the prefix $PREFIX:"
export FXC_CONFIG_DIR="$T/config" FXC_STATE_DIR="$T/state" FXC_OWNER="e2e-$$"
unset FXC_TEAM FXC_PROJECT FXC_PREFIX FXC_VCPUS FXC_IDLE_MINUTES FXC_MAX_AGENTS FXC_LOCAL FXC_SHIM_DRYRUN
if [ -n "$(ours)" ]; then
  bad "sandboxes named $PREFIX* already exist in $FXC_E2E_TEAM/$FXC_E2E_PROJECT; remove them or wait, then try again"
  exit 1
fi

rc=0
"$FXC" ls >"$T/noconfig.out" 2>&1 || rc=$?
check "without settings, a command stops and points to fx-cloud init" test "$rc" = 1 -a "$(count "$T/noconfig.out" "fx-cloud init")" -ge 1
check "init writes the settings" "$FXC" init --team "$FXC_E2E_TEAM" --project "$FXC_E2E_PROJECT" --prefix "$PREFIX"
check "the settings have the test prefix" python3 -c 'import json, sys; sys.exit(json.load(open(sys.argv[1]))["prefix"] != "fxt-")' "$T/config/config.json"
# Prefix filtering against the real list, which holds other tools' sandboxes: with the short
# prefix "fx", the server returns all of them, and fx-cloud must still see none as its own.
FXC_PREFIX=fx "$FXC" ls >"$T/wide-ls.out" 2>&1 || true
check "ls with a wide prefix sees no other tool's sandbox" contains "$T/wide-ls.out" "no agent sandboxes"
FXC_PREFIX=fx "$FXC" gc --all --dry-run >"$T/wide-gc.out" 2>&1 || true
check "gc --all --dry-run with a wide prefix would remove nothing" contains "$T/wide-gc.out" "nothing to remove"

live_started=1
t0=$(now)
rc=0
"$FXC" base --no-warm 2>&1 | tee "$T/base.log" | sed 's/^/    /' || rc=$?
base_s=$(( $(now) - t0 ))
grep -o 'snap_[A-Za-z0-9]*' "$T/base.log" >>"$T/snapshots.txt" || true  # checked at the end
check "base --no-warm builds a base snapshot" test "$rc" = 0 -a -f "$T/config/base.json"

R="$T/repo"
mkdir -p "$R" "$T/state"
git -C "$R" init -q -b main
git -C "$R" -c user.name=Test -c user.email=test@example.com -c commit.gpgsign=false commit -q --allow-empty -m init
echo committed >"$R/file"
git -C "$R" add file
git -C "$R" -c user.name=Test -c user.email=test@example.com -c commit.gpgsign=false commit -q -m file
echo "uncommitted change" >"$R/file"
touch "$T/state/gc.stamp"  # no background gc during the test
cd "$R"

t0=$(now)
rc=0
"$FXC" run 'uname -s; cat file' >"$T/run1.out" 2>"$T/run1.err" || rc=$?
first_s=$(( $(now) - t0 ))
sed 's/^/    /' "$T/run1.err" "$T/run1.out"
check "the first run starts a sandbox and succeeds" test "$rc" = 0
check "it runs on Linux" contains "$T/run1.out" "Linux"
check "it sees the uncommitted change" contains "$T/run1.out" "uncommitted change"

t0=$(now)
rc=0
"$FXC" run 'exit 7' >/dev/null 2>&1 || rc=$?
again_s=$(( $(now) - t0 ))
check "the exit code comes back (exit 7 -> 7)" test "$rc" = 7

echo "edited on the Mac" >"$R/file"
t0=$(now)
"$FXC" run 'cat file' >"$T/run2.out" 2>/dev/null || true
edit_s=$(( $(now) - t0 ))
check "a Mac edit reaches the sandbox" contains "$T/run2.out" "edited on the Mac"

check "a command makes a file in the sandbox" "$FXC" run 'echo made-in-sandbox > made.txt'
check "pull copies it to the Mac" "$FXC" pull made.txt "$T/pulled"
check "with its content" contains "$T/pulled/made.txt" "made-in-sandbox"
"$FXC" pull --patch >"$T/patch" 2>/dev/null || true
check "pull --patch shows the sandbox change" contains "$T/patch" "+made-in-sandbox"
rc=0
"$FXC" run true >/dev/null 2>"$T/changed.err" || rc=$?
check "run refuses to overwrite a changed sandbox copy" test "$rc" = 1 -a "$(count "$T/changed.err" "not on the Mac")" -ge 1
check "pull --patch --apply takes the change" "$FXC" pull --patch --apply
check "the file is on the Mac" contains "$R/made.txt" "made-in-sandbox"
check "then run works again" "$FXC" run 'test -f made.txt'

agent=$(python3 -c 'import os, sys; print([n[:-5] for n in sorted(os.listdir(sys.argv[1])) if n.endswith(".json")][0])' "$T/state/agents" 2>/dev/null || true)
"$FXC" ls >"$T/ls.out" 2>&1 || true
check "ls lists the agent sandbox" contains "$T/ls.out" "$agent"
"$FXC" status >"$T/status.out" 2>&1 || true
check "status shows the base snapshot" contains "$T/status.out" "base snapshot: snap_"

# A stand-in TLA+ runner (no Java) fails and prints its log path; the log copies back to the Mac.
mkdir -p "$R/tests/tla/demo"
cat >"$R/tests/tla/demo/check.sh" <<'EOF'
#!/bin/sh
d="$(mktemp -d "${TMPDIR:-/tmp}/fx-tla-demo.XXXXXX")"
echo "logs: $d"
echo "model error in Demo" > "$d/Demo.log"
mkdir -p "$d/Demo.states" && echo state > "$d/Demo.states/s.log"
echo "FAIL  Demo: expected -, TLC exited 12 (see $d/Demo.log)"
exit 1
EOF
rc=0
"$FXC" tla --tool sh --real /bin/sh -- tests/tla/demo/check.sh >"$T/tla.out" 2>"$T/tla.err" || rc=$?
tla_log=$(sed -n 's/.*(see \(.*\))$/\1/p' "$T/tla.out")
tla_run=$(dirname "$(dirname "$tla_log")")
check "tla exits with the script's exit code (1)" test "$rc" = 1
check "tla prints the sandbox log folder" contains "$T/tla.out" "logs: /tmp/fxc-tla-$(id -u)/"
check "the log from the (see ...) line exists on the Mac" test -f "$tla_log"
check "that log holds the model error" contains "$tla_log" "model error in Demo"
check "the Demo.states folder stays in the sandbox" test -n "$tla_log" -a ! -e "$(dirname "$tla_log")/Demo.states"
check "tla reports the copy on stderr" contains "$T/tla.err" "copied 1 TLC log file to"
case "$tla_run" in
  /tmp/fxc-tla-"$(id -u)"/????????) rm -rf -- "$tla_run" ;;
  *) echo "  not removing $tla_run: not a TLA+ run folder" ;;
esac

check "done removes the sandbox" "$FXC" "done"
gone=0
for _ in 1 2 3 4 5 6; do
  if ! ours | grep -qx "$agent"; then gone=1; break; fi
  sleep 5
done
check "the sandbox is gone from the project" test "$gone" = 1

P="$T/plain"  # a folder outside git: a one-way copy
mkdir -p "$P/node_modules"
echo "plain file" >"$P/notes.txt"
echo "left out" >"$P/node_modules/dep.js"
cd "$P"
"$FXC" run 'cat notes.txt; ls node_modules 2>/dev/null | wc -l' >"$T/plain.out" 2>/dev/null || true
check "a folder outside git is copied one way" contains "$T/plain.out" "plain file"
check "without its dependency folders" test "$(tail -1 "$T/plain.out" | tr -d ' ')" = 0
check "done removes that folder's sandbox too" "$FXC" "done"
cd "$R"
check "base --remove removes the base and its snapshots" "$FXC" base --remove
check "and forgets it" test ! -f "$T/config/base.json"

echo "timings: base --no-warm ${base_s}s, first run (new sandbox) ${first_s}s, run with no change ${again_s}s, run after a Mac edit ${edit_s}s"
