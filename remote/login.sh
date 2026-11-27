#!/usr/bin/env bash
# Runs inside the sandbox, in a terminal. One-time sign-ins that need the user.
# Each step is skipped when it is already done.
set -uo pipefail

WS="${FXC_REMOTE_WS:-/Users/faxes/Developer/Fx}"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:/usr/local/bin:$PATH"
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail=0

# In the base for agent sandboxes (FXC_BASE=1) step 3 is skipped: agents only build and test, and
# signed commits stay on the Mac. gh stays, so agents can fetch internal repositories.
step "1/5 fx login (your fx account, the code lane)"
if fx status --json 2>/dev/null | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("auth") == "fx login" else 1)'; then
  echo "fx is already signed in."
else
  echo "Open the link on your Mac and enter the code."
  FX_NO_OPEN_BROWSER=1 fx login || fail=1
fi
team_of() { fx status 2>/dev/null | sed -n 's/^\[status\] team=//p' | sed -n 1p; }
chosen_file="$HOME/.config/fx-cloud/team-chosen"
team="$(team_of)"
if [ -n "$team" ] && [ "$team" = "$(cat "$chosen_file" 2>/dev/null)" ]; then
  echo "AI Gateway team: $team (your choice). Run fx teams in the sandbox to change it."
elif [ -n "${FXC_MAC_TEAM:-}" ] && [ -n "$team" ] && [ "$team" != "$FXC_MAC_TEAM" ]; then
  echo
  echo "fx here bills model calls to the team '$team'. On your Mac it uses '$FXC_MAC_TEAM'."
  echo "To match the Mac, choose '$FXC_MAC_TEAM' in the list. To keep '$team', choose it again."
  if fx teams; then
    mkdir -p "$(dirname "$chosen_file")"
    team_of > "$chosen_file"
  else
    fail=1
  fi
elif [ -n "$team" ]; then
  echo "AI Gateway team: $team (the same as on your Mac)."
fi

step "2/5 GitHub login"
scopes="repo,read:org,workflow,admin:ssh_signing_key"
# gh cannot open a browser in the sandbox. This helper prints the link instead.
browser="$HOME/.local/share/fx-cloud/print-url"
mkdir -p "$(dirname "$browser")"
cat > "$browser" <<'EOF'
#!/bin/sh
printf '\n  Open this link on your Mac: %s\n  Type the code from above, then approve. Come back here after that.\n\n' "$1"
EOF
chmod +x "$browser"
# Checks read whole outputs into variables: with pipefail, `cmd | grep -q` can fail on SIGPIPE.
if gh auth status --hostname github.com >/dev/null 2>&1; then
  gh_status="$(gh auth status --hostname github.com 2>&1)"
  case "$gh_status" in
    *admin:ssh_signing_key*) echo "gh is already signed in." ;;
    *)
      echo "Adding the admin:ssh_signing_key scope. Use the same code steps as a new login."
      GH_BROWSER="$browser" gh auth refresh --hostname github.com --scopes admin:ssh_signing_key || fail=1
      ;;
  esac
else
  cat <<'EOF'
GitHub signs in with a one-time code:
  1. gh asks "Authenticate Git with your GitHub credentials?". Answer Y.
  2. gh shows a code such as ABCD-1234. Ignore "Failed to copy one-time code to clipboard".
  3. Press Enter. gh prints the link https://github.com/login/device.
  4. Open that link on your Mac, type the code, and approve. If GitHub shows
     vercel-labs under organization access, click Authorize, so fx-web can clone.
  5. Come back here. gh continues by itself.

EOF
  GH_BROWSER="$browser" gh auth login --hostname github.com --git-protocol https --web --scopes "$scopes" || fail=1
fi
gh auth setup-git --hostname github.com || fail=1

step "3/5 Commit signing key on GitHub"
if [ -n "${FXC_BASE:-}" ]; then
  echo "Skipped in the base for agent sandboxes: signed commits stay on the Mac."
else
pub="$(cut -d' ' -f1-2 "$HOME/.ssh/fx_signing.pub")"
signing_keys="$(gh api user/ssh_signing_keys --paginate --jq '.[].key' 2>/dev/null || true)"
if grep -qxF "$pub" <<< "$signing_keys"; then
  echo "The signing key is already on your GitHub account."
elif ! gh auth status --hostname github.com >/dev/null 2>&1; then
  echo "Skipped: GitHub is not signed in."
  fail=1
else
  gh ssh-key add "$HOME/.ssh/fx_signing.pub" --type signing --title "fx-cloud sandbox $(date +%F)" || fail=1
fi
fi

step "4/5 fx-web checkout"
if [ -d "$WS/fx-web/.git" ]; then
  git -C "$WS/fx-web" fetch --quiet origin && echo "fx-web is already cloned."
else
  GIT_TERMINAL_PROMPT=0 git clone --quiet https://github.com/vercel-labs/fx-web.git "$WS/fx-web" \
    && echo "Cloned fx-web." || { echo "Could not clone fx-web. Check the GitHub login and its vercel-labs access."; fail=1; }
fi
if [ -d "$WS/fx-web/.git" ]; then
  git -C "$WS/fx-web" config user.name Pranit
  git -C "$WS/fx-web" config user.email pranit@vercel.com
fi

step "5/5 Test-lane key for aig (optional)"
key_file="$HOME/.config/ai-gateway/project-fx.key"
key_bytes() { [ -f "$key_file" ] && wc -c < "$key_file" | tr -d ' ' || echo 0; }
# A real key is much longer than 20 bytes; a shorter file is a mistyped answer, not a key.
if [ "$(key_bytes)" -ge 20 ]; then
  echo "The test key is already stored."
else
  [ -f "$key_file" ] && echo "The stored test key is not valid (it is too short)."
  printf 'Store the Project fx test key now? Evals and dogfood runs need it. [y/N] '
  read -r answer
  if [ "$answer" = y ] || [ "$answer" = Y ]; then
    echo "Paste the whole key, then press Enter. You will not see it while you paste."
    "$HOME/.local/bin/aig" set-test-key || fail=1
    if [ "$(key_bytes)" -lt 20 ]; then
      echo "That was too short to be the key. Run fx-cloud login again to retry."
      fail=1
    fi
  fi
fi

if [ "$fail" = 0 ]; then
  printf '\nAll sign-ins are done. fx on the Mac can now use fx-cloud run and fx-cloud agent.\n'
else
  printf '\nOne or more steps failed. Run fx-cloud login again to retry them.\n'
fi
exit "$fail"
