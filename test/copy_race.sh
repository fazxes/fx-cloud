#!/bin/sh
# Shows the fx-cloud copy race. N `fx-cloud run true` calls start at the same moment in one git
# worktree whose files differ from the sandbox copy, so each one runs apply_copy on the same
# sandbox folder. Some of them fail with git's index.lock error.
#
#   sh test/copy_race.sh <path to an fx-cloud script> [N, default 20]
#
# The repo is a throwaway folder under REPO_PARENT. The script removes its sandbox and both of its
# folders at the end. Exit status: 0 when no run failed, 1 when a run failed, 2 for bad usage or
# when the setup failed.
set -u

REPO_PARENT=${FXC_RACE_PARENT:-/Users/faxes/Developer/Fx/worktrees}
GATE_POLL_SECONDS=0.05
TAIL_LINES=20

usage() {
  echo "usage: sh test/copy_race.sh <path to an fx-cloud script> [N, default 20]" >&2
  exit 2
}

# run_rc I: the exit code that run I wrote, or "missing"
run_rc() {
  cat "$tmp/rc.$1" 2>/dev/null || echo missing
}

# start_run I: starts run I in the background. It waits for the go file, so all runs start together.
start_run() {
  (
    while [ ! -e "$tmp/go" ]; do
      sleep "$GATE_POLL_SECONDS"
    done
    "$FXC" run true >"$tmp/out.$1" 2>&1
    echo $? >"$tmp/rc.$1"
  ) &
  pids="$pids $!"
}

make_repo() {
  git -C "$1" init -q -b main || return 1
  git -C "$1" config user.name t || return 1
  git -C "$1" config user.email t@example.com || return 1
  git -C "$1" config commit.gpgsign false || return 1
  printf 'one\n' >"$1/a.txt" || return 1
  git -C "$1" add a.txt || return 1
  git -C "$1" commit -q -m init || return 1
  printf 'two\n' >"$1/a.txt" || return 1
  printf 'new\n' >"$1/b.txt" || return 1
}

# fail_setup MESSAGE: removes what this script made so far, then exits 2
fail_setup() {
  cd "$start_dir" || cd /
  rm -rf "$repo"
  if [ -n "${tmp:-}" ]; then
    rm -rf "$tmp"
  fi
  echo "copy_race: $1" >&2
  exit 2
}

[ $# -ge 1 ] && [ $# -le 2 ] || usage
n=${2:-20}
case $n in
  ''|*[!0-9]*) usage ;;
esac
[ "$n" -ge 1 ] || usage

if [ ! -f "$1" ] || [ ! -x "$1" ]; then
  echo "copy_race: not an executable file: $1" >&2
  exit 2
fi
FXC=$(cd "$(dirname "$1")" && pwd -P)/$(basename "$1")
start_dir=$(pwd -P)
tmp=

repo=$REPO_PARENT/fxc-race-$(date -u +%Y%m%dT%H%M%SZ)
if ! mkdir "$repo"; then
  echo "copy_race: could not create $repo (it may already exist)" >&2
  exit 2
fi
make_repo "$repo" || fail_setup "could not set up $repo"
tmp=$(mktemp -d) || fail_setup "could not make a temp folder"
cd "$repo" || fail_setup "could not enter $repo"

pids=
i=1
while [ "$i" -le "$n" ]; do
  start_run "$i"
  i=$((i + 1))
done
: >"$tmp/go"
for p in $pids; do
  wait "$p"
done

failed=0
index_lock=0
i=1
while [ "$i" -le "$n" ]; do
  if [ "$(run_rc "$i")" != 0 ]; then
    failed=$((failed + 1))
  fi
  if grep -qF 'index.lock' "$tmp/out.$i" 2>/dev/null; then
    index_lock=$((index_lock + 1))
  fi
  i=$((i + 1))
done
echo "copy_race: N=$n failed=$failed index_lock=$index_lock"

i=1
while [ "$i" -le "$n" ]; do
  rc=$(run_rc "$i")
  if [ "$rc" != 0 ]; then
    echo "--- run $i exit=$rc"
    tail -n "$TAIL_LINES" "$tmp/out.$i"
  fi
  i=$((i + 1))
done

if ! (cd "$repo" && "$FXC" done) >"$tmp/done.out" 2>&1; then
  echo "copy_race: fx-cloud done failed; its output:" >&2
  cat "$tmp/done.out" >&2
fi
cd "$start_dir" || cd /
rm -rf "$repo" "$tmp"

if [ "$failed" -eq 0 ]; then
  exit 0
fi
exit 1
