#!/bin/sh
# Install fx-cloud: link the command into ~/.local/bin, and add the fx-cloud skill to the agents
# on this computer.
#
#   ./install.sh              install, or update an existing install
#   ./install.sh --shims      also put the zig and bun shims first in PATH (~/.zprofile, ~/.zshrc)
#   ./install.sh --add-rule   also add a short rule to ~/.fx/AGENTS.md, ~/.claude/CLAUDE.md
#                             and ~/.codex/AGENTS.md
#
# Remove it with ./uninstall.sh.
set -eu

if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
  echo "install.sh: HOME is not set to a folder" >&2
  exit 1
fi

here=$(cd "$(dirname "$0")" && pwd -P)
shims=0
add_rule=0
for arg in "$@"; do
  case "$arg" in
    --shims) shims=1 ;;
    --add-rule) add_rule=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

RULE_START='<!-- fx-cloud:start -->'
RULE_END='<!-- fx-cloud:end -->'
SHIM_MARK='# fx-cloud shims'

# Requirements.
command -v python3 >/dev/null 2>&1 || fail "python3 is missing; install Python 3.9 or newer"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || fail "fx-cloud needs Python 3.9 or newer; this python3 is $(python3 -V 2>&1)"
command -v git >/dev/null 2>&1 || fail "git is missing"
command -v vercel >/dev/null 2>&1 \
  || fail "the Vercel CLI is missing; install it with: npm i -g vercel   (then run: vercel login)"

# 1. The command.
bin="$HOME/.local/bin"
mkdir -p "$bin"
if [ -e "$bin/fx-cloud" ] && [ ! -L "$bin/fx-cloud" ]; then
  fail "$bin/fx-cloud is a file, not a link; move it away, then run ./install.sh again"
fi
if [ -L "$bin/fx-cloud" ] && [ "$(readlink "$bin/fx-cloud")" != "$here/fx-cloud" ]; then
  say "Replacing the link $bin/fx-cloud -> $(readlink "$bin/fx-cloud")"
fi
ln -sfn "$here/fx-cloud" "$bin/fx-cloud"
say "Linked $bin/fx-cloud -> $here/fx-cloud"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) say "Note: $bin is not in PATH. Add it to your shell's startup file: export PATH=\"$bin:\$PATH\"" ;;
esac

# 2. The skill, for each agent whose home folder exists.
for tool in fx claude agents; do
  [ -d "$HOME/.$tool" ] || continue
  dest="$HOME/.$tool/skills/fx-cloud"
  mkdir -p "$dest"
  cp "$here/skill/SKILL.md" "$dest/SKILL.md.new" && mv "$dest/SKILL.md.new" "$dest/SKILL.md"
  say "Installed the skill in $dest"
done

# 3. The shims: one line, last in ~/.zprofile and ~/.zshrc, so zig and bun find them first.
if [ "$shims" = 1 ]; then
  line="export PATH=\"$here/shims:\$PATH\"  $SHIM_MARK"
  case "${SHELL:-}" in *zsh) zsh=1 ;; *) zsh=0 ;; esac
  if [ "$zsh" = 1 ] || [ -f "$HOME/.zprofile" ] || [ -f "$HOME/.zshrc" ]; then
    for rc in "$HOME/.zprofile" "$HOME/.zshrc"; do
      if [ -f "$rc" ] && grep -qxF "$line" "$rc"; then
        say "The shims line is already in $rc"
        continue
      fi
      if [ -f "$rc" ] && grep -qF "$SHIM_MARK" "$rc"; then
        tmp=$(mktemp)
        grep -vF "$SHIM_MARK" "$rc" >"$tmp" || true
        cat "$tmp" >"$rc"
        rm -f "$tmp"
      fi
      printf '%s\n' "$line" >>"$rc"
      say "Added the shims line to $rc"
    done
    say "Open a new terminal for the shims. FXC_LOCAL=1 runs zig and bun here for one command."
  else
    say "Add this line to the end of your shell's startup file, so zig and bun find the shims first:"
    say "  $line"
  fi
fi

# 4. The rule, for each agent whose home folder exists.
if [ "$add_rule" = 1 ]; then
  # shellcheck disable=SC2016 # the backquotes are Markdown, not a command
  block=$(printf '%s\n' "$RULE_START" \
    '## Heavy commands run in a Vercel Sandbox' \
    'Run builds, tests, and other heavy commands with `fx-cloud run <command>` from the project folder.' \
    'Keep git, file edits, and short commands on this computer. The fx-cloud skill has the details.' \
    "$RULE_END")
  for pair in fx:AGENTS.md claude:CLAUDE.md codex:AGENTS.md; do
    tool=${pair%%:*}
    file="$HOME/.$tool/${pair#*:}"
    [ -d "$HOME/.$tool" ] || continue
    if [ -f "$file" ] && grep -qF "$RULE_START" "$file"; then
      current=$(awk -v s="$RULE_START" -v e="$RULE_END" '$0 == s {on = 1} on {print} on && $0 == e {on = 0}' "$file")
      if [ "$current" = "$block" ]; then
        say "The rule is already in $file"
        continue
      fi
      tmp=$(mktemp)
      awk -v s="$RULE_START" -v e="$RULE_END" '$0 == s {skip = 1; next} skip {if ($0 == e) skip = 0; next} {print}' "$file" >"$tmp"
      cat "$tmp" >"$file"
      rm -f "$tmp"
    fi
    if [ -s "$file" ]; then printf '\n' >>"$file"; fi
    printf '%s\n' "$block" >>"$file"
    say "Added the rule to $file"
  done
fi

say ""
say "Done. Next:"
say "  fx-cloud init    choose the Vercel team and project for the sandboxes"
say "  fx-cloud base    build the base snapshot (about a minute; 5 to 12 with warm builds)"
