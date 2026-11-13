#!/usr/bin/env bash
# Runs inside the base sandbox. Installs the toolchain and the sandbox half of fx-cloud, from the
# bundle that `fx-cloud base` uploads. Safe to run again: every step checks before it acts.
# Usage: setup.sh <bundle-dir>
set -euo pipefail

B="${1:?bundle dir}"
SHARE="$HOME/.local/share/fx-cloud"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:/usr/local/bin:$PATH"

log() { printf '[setup] %s\n' "$*"; }
fact() {
  python3 - "$B/mac/facts.json" "$1" <<'PY'
import json, sys
v = json.load(open(sys.argv[1])).get(sys.argv[2])
print("" if v is None or v is False else ("1" if v is True else v))
PY
}
MAC_HOME="$(fact home)"
ZIG_VERSION="$(fact zig)"
BUN_VERSION="$(fact bun)"
WANT_FX="$(fact fx)"
FX_CHANNEL="$(fact fx_channel)"
GIT_NAME="$(fact git_name)"
GIT_EMAIL="$(fact git_email)"

# 1. Basic tools.
missing=()
for pair in git:git curl:curl unzip:unzip rg:ripgrep jq:jq tmux:tmux xz:xz-utils; do
  command -v "${pair%%:*}" >/dev/null 2>&1 || missing+=("${pair#*:}")
done
if [ "${#missing[@]}" -gt 0 ]; then
  log "apt-get install ${missing[*]}"
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null
fi
if ! command -v gh >/dev/null 2>&1; then
  log "apt-get install gh"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gh >/dev/null 2>&1 \
    || log "warning: could not install gh; fx-cloud login cannot sign in to GitHub"
fi

# 2. Zig, the version the Mac uses, checked against the SHA-256 in ziglang.org's release index.
if [ -n "$ZIG_VERSION" ] && [ "$(zig version 2>/dev/null || true)" != "$ZIG_VERSION" ]; then
  if read -r url sha < <(curl -fsSL https://ziglang.org/download/index.json | python3 -c '
import json, sys
d = json.load(sys.stdin)
want, key = sys.argv[1], sys.argv[2] + "-linux"
rel = d.get(want) or (d["master"] if d.get("master", {}).get("version") == want else None)
if rel and key in rel:
    print(rel[key]["tarball"], rel[key]["shasum"])
' "$ZIG_VERSION" "$(uname -m)") && [ -n "${url:-}" ]; then
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
  else
    log "warning: zig $ZIG_VERSION is not in ziglang.org's download index; install it with a setup_hook"
  fi
fi
if command -v zig >/dev/null 2>&1; then log "zig $(zig version)"; else log "no zig (the Mac has none)"; fi

# 3. Bun, the version the Mac uses.
if [ -n "$BUN_VERSION" ] && [ "$(bun --version 2>/dev/null || true)" != "$BUN_VERSION" ]; then
  log "bun $BUN_VERSION"
  if curl -fsSL https://bun.sh/install | bash -s "bun-v$BUN_VERSION" >/dev/null 2>&1; then
    sudo ln -sfn "$HOME/.bun/bin/bun" /usr/local/bin/bun
  else
    log "warning: could not install bun $BUN_VERSION"
  fi
fi
if command -v bun >/dev/null 2>&1; then log "bun $(bun --version)"; else log "no bun (the Mac has none)"; fi

# 4. fx, when the Mac has it, on the Mac's release channel, with the Mac's preferences. Later
#    changes in the sandbox stay: a preference is set only when it is missing.
if [ "$WANT_FX" = 1 ]; then
  if [ ! -x "$HOME/.local/bin/fx" ]; then
    log "installing fx"
    curl -fsSL https://fx.sh/setup.sh | bash >/dev/null
  fi
  if [ -n "$FX_CHANNEL" ] && [ "$FX_CHANNEL" != stable ]; then
    "$HOME/.local/bin/fx" upgrade --channel "$FX_CHANNEL" >/dev/null 2>&1 \
      || log "warning: fx upgrade --channel $FX_CHANNEL failed"
  fi
  sudo ln -sfn "$HOME/.local/bin/fx" /usr/local/bin/fx
  log "fx $("$HOME/.local/bin/fx" --version 2>/dev/null || echo '(version unknown)')"
  mkdir -p "$HOME/.fx" && chmod 700 "$HOME/.fx"
  if [ -f "$B/mac/fx-settings.json" ]; then
    python3 - "$B/mac/fx-settings.json" "$HOME/.fx/settings.json" <<'PY'
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
  fi
fi

# 5. Skills, and the agents' global rules with the sandbox note in front. Skills extract over the
#    old copy, so skills added in the sandbox stay.
for root in agents fx claude codex; do
  if [ -f "$B/skills/$root.tgz" ]; then
    mkdir -p "$HOME/.$root/skills"
    tar -xzf "$B/skills/$root.tgz" -C "$HOME/.$root/skills"
  fi
done
mkdir -p "$SHARE"
cp "$B/remote/AGENTS.sandbox.md" "$SHARE/SANDBOX.md"
for f in "$B"/mac/rules/*.md; do
  [ -f "$f" ] || continue
  n="$(basename "$f")"
  tool="${n%%-*}"
  mkdir -p "$HOME/.$tool"
  { cat "$B/remote/AGENTS.sandbox.md"; if [ -s "$f" ]; then echo; cat "$f"; fi; } > "$HOME/.$tool/${n#*-}"
done

# 6. The Mac's home folder, so copies of its folders live at the same path here.
if [ -n "$MAC_HOME" ] && [ "${MAC_HOME#/}" != "$MAC_HOME" ] && [ "$MAC_HOME" != / ] && [ "$MAC_HOME" != "$HOME" ]; then
  sudo mkdir -p "$MAC_HOME"
  sudo chown "$(id -u):$(id -g)" "$MAC_HOME"
  log "copies go under $MAC_HOME, as on the Mac"
fi

# 7. Git: the Mac's name and email, and no signing key. Commits stay on the Mac.
[ -z "$GIT_NAME" ] || git config --global user.name "$GIT_NAME"
[ -z "$GIT_EMAIL" ] || git config --global user.email "$GIT_EMAIL"
git config --global init.defaultBranch main
if gh auth status --hostname github.com >/dev/null 2>&1; then
  gh auth setup-git --hostname github.com >/dev/null 2>&1 || true
fi

# 8. The sandbox half of fx-cloud.
install -m 755 "$B/remote/fxc-remote.py" "$SHARE/fxc-remote.py"
install -m 755 "$B/remote/login.sh" "$SHARE/login.sh"

# 9. The user's own setup_hook: extra packages, tools, sign-ins.
if [ -f "$B/hook/setup-hook" ]; then
  log "running the setup_hook"
  FXC_BUNDLE="$B" bash "$B/hook/setup-hook"
fi

log "done"
