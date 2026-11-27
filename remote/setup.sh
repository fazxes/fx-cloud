#!/usr/bin/env bash
# Runs inside the sandbox. Safe to run again: every step checks before it acts.
# Usage: setup.sh <bundle-dir> [--no-warm]
set -euo pipefail

B="${1:?bundle dir}"
WARM=1
[ "${2:-}" = "--no-warm" ] && WARM=0
WS="${FXC_REMOTE_WS:-/Users/faxes/Developer/Fx}"
ZIG_VERSION=0.17.1
BUN_VERSION=1.3.14
SHARE="$HOME/.local/share/fx-cloud"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:/usr/local/bin:$PATH"

log() { printf '[setup] %s\n' "$*"; }

# 1. System packages (CI's E2E job needs tmux, zsh and fish).
missing=()
command -v zsh >/dev/null 2>&1 || missing+=(zsh)
command -v fish >/dev/null 2>&1 || missing+=(fish)
command -v unzip >/dev/null 2>&1 || missing+=(unzip)
command -v rg >/dev/null 2>&1 || missing+=(ripgrep)
command -v jq >/dev/null 2>&1 || missing+=(jq)
command -v ssh-keygen >/dev/null 2>&1 || missing+=(openssh-client)
command -v tmux >/dev/null 2>&1 || missing+=(tmux)
command -v java >/dev/null 2>&1 || missing+=(openjdk-21-jre-headless)
if [ "${#missing[@]}" -gt 0 ]; then
  log "apt-get install ${missing[*]}"
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null
fi
# Same as CI: Ubuntu's global zshrc runs compinit, which breaks the zsh profile tests.
if ! grep -qs skip_global_compinit /etc/zsh/zshenv; then
  echo 'skip_global_compinit=1' | sudo tee -a /etc/zsh/zshenv >/dev/null
fi
# Same as CI, so E2E tests see the same tmux defaults.
printf 'set-option -g history-limit 100000\n' > "$HOME/.tmux.conf"

# 2. Zig, checked against the SHA-256 in ziglang.org's release index.
if [ "$(zig version 2>/dev/null || true)" != "$ZIG_VERSION" ]; then
  arch="$(uname -m)"
  read -r url sha < <(curl -fsSL https://ziglang.org/download/index.json | python3 -c '
import json, sys
d = json.load(sys.stdin)[sys.argv[1]][sys.argv[2] + "-linux"]
print(d["tarball"], d["shasum"])' "$ZIG_VERSION" "$arch")
  log "zig $ZIG_VERSION from $url"
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/zig.tar.xz" "$url"
  echo "$sha  $tmp/zig.tar.xz" | sha256sum -c --quiet -
  sudo mkdir -p /opt/zig
  sudo tar -xJf "$tmp/zig.tar.xz" -C /opt/zig
  dir="$(basename "$url" .tar.xz)"
  [ -x "/opt/zig/$dir/zig" ] || { log "unexpected tarball layout in $url"; exit 1; }
  sudo ln -sfn "/opt/zig/$dir/zig" /usr/local/bin/zig
  rm -rf "$tmp"
fi
log "zig $(zig version)"

# 3. Bun, the version the E2E job pins.
if [ "$(bun --version 2>/dev/null || true)" != "$BUN_VERSION" ]; then
  log "bun $BUN_VERSION"
  curl -fsSL https://bun.sh/install | bash -s "bun-v$BUN_VERSION" >/dev/null 2>&1
  sudo ln -sfn "$HOME/.bun/bin/bun" /usr/local/bin/bun
fi
log "bun $(bun --version)"

# 3b. TLA+ tools for the model checks, pinned as in CI. The check scripts and Bun drivers look
# for the jar at ~/.local/share/tla/tla2tools.jar when TLA2TOOLS_JAR is unset; set it as well.
TLA_JAR="$HOME/.local/share/tla/tla2tools.jar"
TLA_SHA=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
if ! echo "$TLA_SHA  $TLA_JAR" | sha256sum -c --quiet - >/dev/null 2>&1; then
  log "tla2tools.jar v1.7.4"
  mkdir -p "$(dirname "$TLA_JAR")"
  curl -fsSL -o "$TLA_JAR.tmp" https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar
  echo "$TLA_SHA  $TLA_JAR.tmp" | sha256sum -c --quiet -
  mv "$TLA_JAR.tmp" "$TLA_JAR"
fi
echo "export TLA2TOOLS_JAR=$TLA_JAR" | sudo tee /etc/profile.d/fx-cloud-tla.sh >/dev/null
log "tla2tools v1.7.4, $(java -version 2>&1 | head -1)"

# 4. fx on the dev channel, the same channel as the Mac.
if [ ! -x "$HOME/.local/bin/fx" ]; then
  log "installing fx"
  curl -fsSL https://fx.sh/setup.sh | bash >/dev/null
fi
"$HOME/.local/bin/fx" upgrade --channel dev >/dev/null 2>&1 || log "warning: fx upgrade --channel dev failed"
sudo ln -sfn "$HOME/.local/bin/fx" /usr/local/bin/fx
log "fx $("$HOME/.local/bin/fx" --version 2>/dev/null)"

# 5. fx profile: copy the Mac's preferences without overwriting later sandbox changes.
mkdir -p -m 700 "$HOME/.fx"
python3 - "$B/mac/settings.json" "$HOME/.fx/settings.json" <<'PY'
import json, os, sys
src, dst = sys.argv[1], sys.argv[2]
want = json.load(open(src))
have = json.load(open(dst)) if os.path.exists(dst) else {}
for k, v in want.items():
    have.setdefault(k, v)
tmp = dst + ".tmp"
with open(tmp, "w") as f:
    json.dump(have, f, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, dst)
PY
cp "$B/mac/global-AGENTS.md" "$HOME/.fx/AGENTS.md"

# 6. aig, the billing-lane helper, unchanged from the Mac. The test key is not copied.
mkdir -p "$HOME/.local/bin"
install -m 755 "$B/mac/aig" "$HOME/.local/bin/aig"
sudo ln -sfn "$HOME/.local/bin/aig" /usr/local/bin/aig

# 7. Personal skills. Extract over the old copy so skills added in the sandbox stay.
for root in agents fx claude codex; do
  if [ -f "$B/skills/$root.tgz" ]; then
    mkdir -p "$HOME/.$root/skills"
    tar -xzf "$B/skills/$root.tgz" -C "$HOME/.$root/skills"
  fi
done

# 8. Workspace at the same path as on the Mac, so sessions keep their workspace.
if [ ! -d "$WS" ]; then
  sudo mkdir -p "$WS"
  sudo chown -R "$(id -u):$(id -g)" "/$(echo "$WS" | cut -d/ -f2)"
fi
mkdir -p "$WS/worktrees"
cat "$B/remote/AGENTS.sandbox.md" "$B/mac/workspace-AGENTS.md" > "$WS/AGENTS.md"
[ -f "$B/mac/fx.json" ] && cp "$B/mac/fx.json" "$WS/.fx.json"

# 9. Git identity, and SSH commit signing except in the base for agent sandboxes (FXC_BASE=1):
#    agents build and test there, and commits stay on the Mac.
git config --global user.name Pranit
git config --global user.email pranit@vercel.com
if [ -z "${FXC_BASE:-}" ]; then
  mkdir -p -m 700 "$HOME/.ssh"
  if [ ! -f "$HOME/.ssh/fx_signing" ]; then
    log "new signing key ~/.ssh/fx_signing"
    ssh-keygen -q -t ed25519 -N '' -C 'pranit@vercel.com fx-cloud signing' -f "$HOME/.ssh/fx_signing"
  fi
  printf 'pranit@vercel.com %s\n' "$(cut -d' ' -f1-2 "$HOME/.ssh/fx_signing.pub")" > "$HOME/.ssh/allowed_signers"
  git config --global gpg.format ssh
  git config --global user.signingkey "$HOME/.ssh/fx_signing.pub"
  git config --global gpg.ssh.allowedSignersFile "$HOME/.ssh/allowed_signers"
  git config --global commit.gpgsign true
  git config --global tag.gpgsign true
fi
git config --global init.defaultBranch main
git config --global push.autoSetupRemote true

# 10. Canonical fx checkout. fx-web is internal, so login.sh clones it after gh login.
if [ ! -d "$WS/fx/.git" ]; then
  log "cloning vercel-labs/fx"
  git clone --quiet https://github.com/vercel-labs/fx.git "$WS/fx"
fi
git -C "$WS/fx" config user.name Pranit
git -C "$WS/fx" config user.email pranit@vercel.com
git -C "$WS/fx" fetch --quiet origin

# 11. Helper used by fx-cloud sync.
mkdir -p "$SHARE"
install -m 755 "$B/remote/fxc-remote.py" "$SHARE/fxc-remote.py"
install -m 755 "$B/remote/login.sh" "$SHARE/login.sh"

# 12. Warm the global Zig cache with one build of origin/main, which also proves the toolchain.
if [ "$WARM" = 1 ]; then
  warm="/tmp/fxc-warm"
  git -C "$WS/fx" worktree remove --force "$warm" >/dev/null 2>&1 || true
  git -C "$WS/fx" worktree add --quiet --detach "$warm" origin/main
  log "warm build of origin/main ($(git -C "$warm" rev-parse --short HEAD))"
  start=$SECONDS
  (cd "$warm" && zig build)
  log "warm build ok in $((SECONDS - start))s: $("$warm/zig-out/bin/fx" --version)"
  git -C "$WS/fx" worktree remove --force "$warm"
fi

log "done"
