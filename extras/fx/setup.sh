#!/usr/bin/env bash
# A setup_hook for work on fx itself. It adds what fx's tests and CI expect to the base:
# Java 21 with tla2tools.jar for the TLA+ model checks, zsh and fish for the shell tests, and the
# tmux defaults of CI's E2E job. Set it in ~/.config/fx-cloud/config.json:
#   "setup_hook": "<this repository>/extras/fx/setup.sh"
set -euo pipefail

log() { printf '[fx extras] %s\n' "$*"; }

# 1. System packages.
missing=()
command -v zsh >/dev/null 2>&1 || missing+=(zsh)
command -v fish >/dev/null 2>&1 || missing+=(fish)
command -v java >/dev/null 2>&1 || missing+=(openjdk-21-jre-headless)
if [ "${#missing[@]}" -gt 0 ]; then
  log "apt-get install ${missing[*]}"
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null
fi

# 2. Same as CI: Ubuntu's global zshrc runs compinit, which breaks the zsh profile tests.
if ! grep -qs skip_global_compinit /etc/zsh/zshenv; then
  echo 'skip_global_compinit=1' | sudo tee -a /etc/zsh/zshenv >/dev/null
fi

# 3. Same as CI, so E2E tests see the same tmux defaults.
printf 'set-option -g history-limit 100000\n' > "$HOME/.tmux.conf"

# 4. TLA+ tools for the model checks, pinned as in CI. The check scripts and Bun drivers look for
#    the jar at ~/.local/share/tla/tla2tools.jar when TLA2TOOLS_JAR is unset; set it as well.
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
