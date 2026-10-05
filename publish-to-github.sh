#!/usr/bin/env bash
# Create the GitHub repository and push this tree as the first commit.
# Requires the GitHub CLI, already signed in:  gh auth login
#
# Usage: ./publish-to-github.sh [repo-name] [--public]
# Default is a PRIVATE repository. Pass --public only when you mean it.
set -euo pipefail
name="${1:-ipMontior}"
visibility="--private"
[[ "${2:-}" == "--public" ]] && visibility="--public"

command -v gh >/dev/null || { echo "GitHub CLI (gh) not found"; exit 1; }
gh auth status >/dev/null || { echo "run: gh auth login"; exit 1; }
owner="$(gh api user --jq .login)"

echo "About to create ${owner}/${name} (${visibility#--}) and push this directory."
read -r -p "Proceed? [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "aborted, nothing created"; exit 0; }

if [ ! -d .git ]; then
  git init -q -b main
  git add -A
  git commit -q -m "Initial import of Observe"
fi
gh repo create "${owner}/${name}" "${visibility}" \
  --description "Self-hosted homelab availability monitor (SNMP, WinRM/WMI, MQTT, HTTP, DNS, TLS, ICMP)" \
  --source . --remote origin --push
echo "done: https://github.com/${owner}/${name}"
