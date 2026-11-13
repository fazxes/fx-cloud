#!/bin/sh
# Remove fx-cloud from this computer: the command link, the skill, the shims line and the rule.
#
#   ./uninstall.sh          keep the settings and state, in case you install it again
#   ./uninstall.sh --purge  delete them too
#
# The sandboxes and the base snapshot in your Vercel project are not touched. Remove them first
# with `fx-cloud gc --all` and `fx-cloud base --remove`.
set -eu

if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
  echo "uninstall.sh: HOME is not set to a folder" >&2
  exit 1
fi

here=$(cd "$(dirname "$0")" && pwd -P)
purge=0
for arg in "$@"; do
  case "$arg" in
    --purge) purge=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "uninstall.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

RULE_START='<!-- fx-cloud:start -->'
RULE_END='<!-- fx-cloud:end -->'
SHIM_MARK='# fx-cloud shims'
config="${FXC_CONFIG_DIR:-$HOME/.config/fx-cloud}"
state="${FXC_STATE_DIR:-$HOME/.cache/fx-cloud}"
if [ "$purge" = 1 ] && [ -f "$config/base.json" ]; then
  echo "uninstall.sh: $config/base.json names a base snapshot that still exists in Vercel." >&2
  echo "Remove it first: $here/fx-cloud gc --all && $here/fx-cloud base --remove" >&2
  echo "(or delete $config/base.json yourself to forget it), then run ./uninstall.sh --purge again." >&2
  exit 1
fi

# Writes through symlinks, so a startup file that links into a dotfiles folder stays a link.
rewrite() {
  tmp=$(mktemp)
  cat >"$tmp"
  cat "$tmp" >"$1"
  rm -f "$tmp"
}

link="$HOME/.local/bin/fx-cloud"
if [ -L "$link" ] && [ "$(readlink "$link")" = "$here/fx-cloud" ]; then
  rm -f "$link"
  echo "Removed $link"
elif [ -e "$link" ] || [ -L "$link" ]; then
  echo "Left $link: it is not a link to $here/fx-cloud"
fi

for tool in fx claude agents; do
  dir="$HOME/.$tool/skills/fx-cloud"
  if [ -f "$dir/SKILL.md" ] && grep -q '^name: fx-cloud$' "$dir/SKILL.md"; then
    rm -rf "$dir"
    echo "Removed $dir"
  fi
done

for rc in "$HOME/.zprofile" "$HOME/.zshrc"; do
  if [ -f "$rc" ] && grep -qF "$SHIM_MARK" "$rc"; then
    { grep -vF "$SHIM_MARK" "$rc" || true; } | rewrite "$rc"
    echo "Removed the shims line from $rc"
  fi
done

for file in "$HOME/.fx/AGENTS.md" "$HOME/.claude/CLAUDE.md" "$HOME/.codex/AGENTS.md"; do
  if [ -f "$file" ] && grep -qF "$RULE_START" "$file"; then
    # Drops the block and the blank lines that end up at the end of the file.
    awk -v s="$RULE_START" -v e="$RULE_END" '
      $0 == s {skip = 1; next}
      skip {if ($0 == e) skip = 0; next}
      /^$/ {blank++; next}
      {while (blank > 0) {print ""; blank--} print}' "$file" | rewrite "$file"
    echo "Removed the rule from $file"
  fi
done

if [ -f "$config/base.json" ]; then
  echo "Note: $config/base.json names a base snapshot that still exists in Vercel."
  echo "      Remove it with: $here/fx-cloud gc --all && $here/fx-cloud base --remove"
fi
if [ "$purge" = 1 ]; then
  rm -rf "$config" "$state"
  echo "Removed fx-cloud and its settings and state ($config, $state)."
else
  echo "Removed fx-cloud. Its settings are still in $config (delete them with --purge)."
fi
