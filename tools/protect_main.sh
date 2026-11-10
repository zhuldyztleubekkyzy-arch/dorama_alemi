#!/usr/bin/env bash
# Қолдану: GH_TOKEN=... ./tools/protect_main.sh OWNER/REPO
set -euo pipefail
REPO="$1"
gh api -X PUT "repos/$REPO/branches/main/protection" --input - <<JSON
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Lint, Test, Build", "Dependency vulnerability scan (OSV + Trivy)"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": true
  },
  "required_conversation_resolution": true,
  "required_linear_history": true,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
