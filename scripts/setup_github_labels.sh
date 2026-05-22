#!/usr/bin/env bash

set -euo pipefail

REPO="${REPO:-}"

if [[ -z "${REPO}" ]]; then
  remote_url="$(git config --get remote.origin.url 2>/dev/null || true)"
  if [[ "${remote_url}" =~ github.com[:/]([^/]+/[^/.]+)(\.git)?$ ]]; then
    REPO="${BASH_REMATCH[1]}"
  fi
fi

if [[ -z "${REPO}" ]]; then
  echo "Could not determine GitHub repo. Set REPO=owner/name."
  exit 1
fi

declare -a LABELS=(
  "agent:dev|1D76DB|Developer owns implementation"
  "agent:qa|FBCA04|QA validation in progress"
  "agent:planner-review|8250DF|Planner final scope review"
  "agent:changes-requested|D1242F|Blocked until changes are made"
  "agent:ready-to-merge|1A7F37|All agent gates passed"
)

for entry in "${LABELS[@]}"; do
  IFS="|" read -r name color description <<< "${entry}"
  gh label create "${name}" \
    --repo "${REPO}" \
    --color "${color}" \
    --description "${description}" \
    --force
done

echo "Labels synced for ${REPO}"

