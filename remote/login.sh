#!/usr/bin/env bash
# Runs inside the base sandbox, in a terminal. Optional one-time sign-ins: each step asks first,
# and is skipped when it is already done. `fx-cloud login` then saves them in the base snapshot.
set -uo pipefail

export PATH="$HOME/.local/bin:$HOME/.bun/bin:/usr/local/bin:$PATH"
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ask() {
  printf '%s [Y/n] ' "$1"
  read -r answer || answer=n
  case "$answer" in n|N|no|No|NO) return 1 ;; *) return 0 ;; esac
}
fail=0

step "GitHub"
if ! command -v gh >/dev/null 2>&1; then
  echo "gh is not installed here, so this step is skipped."
elif gh auth status --hostname github.com >/dev/null 2>&1; then
  echo "gh is already signed in."
elif ask "Sign in to GitHub, so sandboxes can fetch your private repositories?"; then
  # gh cannot open a browser in the sandbox. This helper prints the link instead.
  browser="$HOME/.local/share/fx-cloud/print-url"
  mkdir -p "$(dirname "$browser")"
  cat > "$browser" <<'EOF'
#!/bin/sh
printf '\n  Open this link on your Mac: %s\n  Type the code from above, then approve. Come back here after that.\n\n' "$1"
EOF
  chmod +x "$browser"
  cat <<'EOF'
GitHub signs in with a one-time code:
  1. If gh asks "Authenticate Git with your GitHub credentials?", answer Y.
  2. gh shows a code such as ABCD-1234. Ignore "Failed to copy one-time code to clipboard".
  3. Press Enter. gh prints the link https://github.com/login/device.
  4. Open that link on your Mac, type the code, and approve. Give access to the
     organizations whose private repositories you build here.
  5. Come back here. gh continues by itself.

EOF
  GH_BROWSER="$browser" gh auth login --hostname github.com --git-protocol https --web || fail=1
else
  echo "Skipped."
fi
if command -v gh >/dev/null 2>&1 && gh auth status --hostname github.com >/dev/null 2>&1; then
  gh auth setup-git --hostname github.com || fail=1
fi

step "fx"
if ! command -v fx >/dev/null 2>&1; then
  echo "fx is not installed here (the base installs it when your Mac has it), so this step is skipped."
elif fx status --json 2>/dev/null | python3 -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("auth") == "fx login" else 1)'; then
  echo "fx is already signed in."
elif ask "Sign in to fx, so fx-cloud agent can use your fx account?"; then
  echo "Open the link on your Mac and enter the code."
  FX_NO_OPEN_BROWSER=1 fx login || fail=1
else
  echo "Skipped."
fi

if [ "$fail" = 0 ]; then
  printf '\nDone. fx-cloud saves these sign-ins in the base snapshot now.\n'
else
  printf '\nOne or more steps failed. Run fx-cloud login again to retry them.\n'
fi
exit "$fail"
