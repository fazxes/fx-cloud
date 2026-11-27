#!/usr/bin/env bash
# Runs inside the base sandbox after setup.sh. It makes the image that every agent sandbox starts
# from: the toolchain, the fx login, the aig test key, the gh login (to fetch internal repos such
# as fx-web) and warm caches stay; the commit signing key and earlier work go, because agents only
# build and test there, and commits stay on the Mac.
# Usage: base.sh [--no-warm]
set -euo pipefail

WARM=1
[ "${1:-}" = "--no-warm" ] && WARM=0
WS="${FXC_REMOTE_WS:-/Users/faxes/Developer/Fx}"
MAC_HOME="/$(echo "$WS" | cut -d/ -f2)/$(echo "$WS" | cut -d/ -f3)"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:/usr/local/bin:$PATH"
log() { printf '[base] %s\n' "$*"; }

# 1. No commit signing key: an agent sandbox cannot make signed commits as the user.
rm -f "$HOME/.ssh/fx_signing" "$HOME/.ssh/fx_signing.pub" "$HOME/.ssh/allowed_signers"
for key in user.signingkey gpg.format gpg.ssh.allowedSignersFile commit.gpgsign tag.gpgsign; do
  git config --global --unset-all "$key" >/dev/null 2>&1 || true
done
log "removed the signing key"

# 2. No earlier work: worktree, review and plain-folder copies, runs, and fx sessions.
if [ -d "$MAC_HOME" ]; then
  find "$MAC_HOME" -mindepth 1 -maxdepth 1 ! -name Developer -exec rm -rf {} +
  find "$MAC_HOME/Developer" -mindepth 1 -maxdepth 1 ! -name "$(basename "$WS")" -exec rm -rf {} +
fi
find "$WS" -mindepth 1 -maxdepth 1 ! -name fx ! -name fx-web ! -name AGENTS.md ! -name .fx.json -exec rm -rf {} +
mkdir -p "$WS/worktrees"
for d in /private/tmp /private/var; do [ -d "$d" ] && sudo rm -rf "$d"; done
rm -rf "$HOME/.fx/sessions" "$HOME/.cache/fx-cloud"
log "removed earlier copies and sessions"

# 3. The canonical clones at origin/main, so a copy sends only the commits after it.
git -C "$WS/fx" fetch --quiet origin
git -C "$WS/fx" checkout --quiet --force --detach origin/main
git -C "$WS/fx" clean -qfdx
git -C "$WS/fx" worktree prune
if gh auth status --hostname github.com >/dev/null 2>&1; then
  gh auth setup-git --hostname github.com >/dev/null 2>&1 || true
  if [ ! -d "$WS/fx-web/.git" ]; then
    GIT_TERMINAL_PROMPT=0 git clone --quiet https://github.com/vercel-labs/fx-web.git "$WS/fx-web" \
      || log "warning: could not clone fx-web"
  fi
fi
if [ -d "$WS/fx-web/.git" ]; then
  if GIT_TERMINAL_PROMPT=0 git -C "$WS/fx-web" fetch --quiet origin; then
    git -C "$WS/fx-web" checkout --quiet --force --detach origin/main
    git -C "$WS/fx-web" clean -qfdx
  else
    log "warning: could not fetch fx-web (is gh signed in? run fx-cloud login); its copies send more history"
  fi
  git -C "$WS/fx-web" worktree prune
fi
log "fx at $(git -C "$WS/fx" rev-parse --short HEAD)$([ -d "$WS/fx-web/.git" ] && echo ", fx-web at $(git -C "$WS/fx-web" rev-parse --short HEAD)")"

# 4. Warm caches. Zig keeps almost all of its work in the project's .zig-cache, not in its global
#    cache. So build fx origin/main the ways agents build it, and keep that .zig-cache as a seed:
#    fxc-remote.py copies it into a copy of fx that has none yet, and only the changed code
#    compiles again. Bun's cache gets the E2E dependencies.
if [ "$WARM" = 1 ]; then
  seeds="$HOME/.cache/fxc-zig-seed"
  rm -rf "$HOME/.cache/zig" "$seeds"
  warm=/tmp/fxc-warm
  git -C "$WS/fx" worktree add --quiet --detach "$warm" origin/main
  start=$SECONDS
  warn() { log "warning: the warm build '$*' failed, so that build starts cold in agent sandboxes"; }
  (
    cd "$warm"
    zig build                                                          # Linux, also before bun test
    # zig build on the Mac. The install folder is part of fx's cache key, so it must be the one
    # that the Mac's zig script and macos-build use (build_out in fx-cloud).
    zig build -Dtarget=aarch64-macos --prefix zig-out/.fx-cloud-aarch64-macos || warn "macOS"
    zig build install-test || warn "install-test"                      # the unit-test binary
    target="$(ls tests/fuzz/corpus 2>/dev/null | head -1)"
    if [ -n "$target" ]; then
      zig build fuzz -Dfuzz-target="$target" >/dev/null 2>&1 || warn "fuzz $target"
    fi
    cd tests/e2e && bun install --frozen-lockfile >/dev/null
  )
  # Not the fuzz inputs or the test scratch files, only the build results. The folders stay,
  # because Zig expects tmp/ ("configuration failed: FileNotFound" without it).
  for d in f tmp; do
    if [ -d "$warm/.zig-cache/$d" ]; then find "$warm/.zig-cache/$d" -mindepth 1 -delete; fi
  done
  mkdir -p "$warm/.zig-cache/tmp"
  mkdir -p "$seeds/fx"
  mv "$warm/.zig-cache" "$seeds/fx/cache"
  fp="$(grep -o -E '\.fingerprint *= *0x[0-9a-fA-F]+' "$warm/build.zig.zon" | grep -o -E '0x[0-9a-fA-F]+' | tr 'A-F' 'a-f')"
  # The CPU model matters: Zig compiles build.zig into native programs, and fx-cloud base adds
  # them for the other models that agent sandboxes land on (add_cpu_seeds).
  cpu="$(cd /tmp && zig build-exe --show-builtin 2>/dev/null | grep -m1 -o 'cpu\.[a-z0-9_]*' | cut -d. -f2)"
  printf '{"fingerprint": "%s", "commit": "%s", "made": %d, "cpus": ["%s"]}\n' \
    "$fp" "$(git -C "$warm" rev-parse --short HEAD)" "$(date +%s)" "$cpu" > "$seeds/fx/meta.json"
  log "warm builds (Linux, macOS, unit tests, fuzz) and E2E install of origin/main in $((SECONDS - start)) s; Zig cache seed $(du -sh "$seeds/fx/cache" | cut -f1), fingerprint $fp"
  git -C "$WS/fx" worktree remove --force "$warm"
fi

log "home $(du -sh "$HOME" 2>/dev/null | cut -f1), workspace $(du -sh "$WS" 2>/dev/null | cut -f1), disk used $(df -h / | awk 'NR==2 {print $3}')"
# Last: /tmp, which also holds this script and the setup bundle.
sudo find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
log "done"
