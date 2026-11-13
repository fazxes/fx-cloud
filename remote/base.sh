#!/usr/bin/env bash
# Runs inside the base sandbox after setup.sh. It makes the image that every agent sandbox starts
# from: the toolchain, the sign-ins, the `warm` repositories and their warm caches stay; earlier
# copies and runs go.
# Usage: base.sh <bundle-dir> [--no-warm]
set -euo pipefail

B="${1:?bundle dir}"
WARM=1
[ "${2:-}" = "--no-warm" ] && WARM=0
SHARE="$HOME/.local/share/fx-cloud"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:/usr/local/bin:$PATH"
log() { printf '[base] %s\n' "$*"; }
MAC_HOME="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("home") or "")' "$B/mac/facts.json")"

# 1. No earlier work: copies of Mac folders, runs, plain-folder state, and agent sessions.
if [ -n "$MAC_HOME" ] && [ -d "$MAC_HOME" ] && [ "$MAC_HOME" != / ] && [ "$MAC_HOME" != "$HOME" ]; then
  find "$MAC_HOME" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
fi
for d in /private/tmp /private/var; do if [ -d "$d" ]; then sudo rm -rf "$d"; fi; done
rm -rf "$HOME/.cache/fx-cloud/plain" "$HOME/.fx/sessions"
for repo in "$HOME"/.cache/fx-cloud/repos/*.git; do
  if [ -d "$repo" ]; then git -C "$repo" worktree prune; fi
done
log "removed earlier copies and runs"

# 2. The `warm` repositories: each is cloned once, so a copy of it sends only the commits after
#    the base. With warm builds, each one's commands run at its default branch, and when that
#    leaves a .zig-cache, the cache becomes a seed: fxc-remote.py copies it into a copy of the same
#    project (by its build.zig.zon fingerprint) that has none yet, so only the changed code
#    compiles again.
seeds="$HOME/.cache/fxc-zig-seed"
if [ "$WARM" = 1 ]; then rm -rf "$HOME/.cache/zig" "$seeds"; fi
for d in "$B"/mac/warm/*/; do
  [ -f "$d/repo" ] || continue
  url="$(head -1 "$d/repo")"
  repo="$(python3 "$SHARE/fxc-remote.py" repo "$url")"
  if ! GIT_TERMINAL_PROMPT=0 git -C "$repo" fetch --quiet origin; then
    log "warning: could not fetch $url (for a private repository, run fx-cloud login, then fx-cloud base)"
    continue
  fi
  git -C "$repo" remote set-head origin --auto >/dev/null 2>&1 || true
  head=refs/remotes/origin/HEAD
  for ref in refs/remotes/origin/HEAD refs/remotes/origin/main refs/remotes/origin/master; do
    if git -C "$repo" rev-parse -q --verify "$ref^{commit}" >/dev/null; then head="$ref"; break; fi
  done
  commit="$(git -C "$repo" rev-parse --short "$head")"
  log "$url at $commit"
  [ "$WARM" = 1 ] || continue
  name="$(basename "$repo" .git)"
  w="/tmp/fxc-warm-$name"
  git -C "$repo" worktree remove --force "$w" >/dev/null 2>&1 || rm -rf "$w"
  git -C "$repo" worktree add --quiet --detach "$w" "$head"
  start=$SECONDS
  cpu_seed=""
  while IFS= read -r cmd || [ -n "$cmd" ]; do
    [ -n "$cmd" ] || continue
    log "warm build: $cmd"
    if (cd "$w" && bash -lc "$cmd" </dev/null); then
      # The first cross build is cheap on another CPU model, so it makes that model's build
      # programs for fx-cloud base's CPU seeds.
      case "$cmd" in *"zig build"*-Dtarget=*) [ -n "$cpu_seed" ] || cpu_seed="$cmd" ;; esac
    else
      log "warning: '$cmd' failed, so it starts cold in agent sandboxes"
    fi
  done < "$d/run"
  fp=""
  if [ -f "$w/build.zig.zon" ]; then
    fp="$(grep -o -E '\.fingerprint *= *0x[0-9a-fA-F]+' "$w/build.zig.zon" | grep -o -E '0x[0-9a-fA-F]+' | tr 'A-F' 'a-f' || true)"
  fi
  if [ -d "$w/.zig-cache" ] && [ -n "$fp" ]; then
    # Only the build results: not fuzz inputs or test scratch files. The folders stay, because
    # Zig expects tmp/ ("configuration failed: FileNotFound" without it).
    for sub in f tmp; do
      if [ -d "$w/.zig-cache/$sub" ]; then find "$w/.zig-cache/$sub" -mindepth 1 -delete; fi
    done
    mkdir -p "$w/.zig-cache/tmp" "$seeds/$name"
    mv "$w/.zig-cache" "$seeds/$name/cache"
    # The CPU model matters: Zig compiles build.zig into native programs, and fx-cloud base adds
    # them for the other models that agent sandboxes land on.
    cpu="$(cd /tmp && zig build-exe --show-builtin 2>/dev/null | grep -m1 -o 'cpu\.[a-z0-9_]*' | cut -d. -f2 || true)"
    python3 - "$seeds/$name/meta.json" "$fp" "$commit" "$cpu" "$repo" "$cpu_seed" "$url" "$name" <<'PY'
import json, sys, time
path, fp, commit, cpu, repo, cpu_seed, url, name = sys.argv[1:]
json.dump({"fingerprint": fp, "commit": commit, "made": int(time.time()), "cpus": [cpu] if cpu else [],
           "repo": repo, "cpu_seed": cpu_seed or "zig build --help", "url": url,
           "name": name.split("__")[-1]}, open(path, "w"))
PY
    log "warm builds of $url in $((SECONDS - start)) s; Zig cache seed $(du -sh "$seeds/$name/cache" | cut -f1), fingerprint $fp"
  else
    log "warm builds of $url in $((SECONDS - start)) s"
  fi
  git -C "$repo" worktree remove --force "$w"
done

log "home $(du -sh "$HOME" 2>/dev/null | cut -f1), disk used $(df -h / | awk 'NR==2 {print $3}')"
# Last: /tmp, which also holds this script and the setup bundle.
sudo find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
log "done"
