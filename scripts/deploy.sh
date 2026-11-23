#!/usr/bin/env bash
# Thin wrapper around scripts/deploy.py (the release pipeline lives there, shared with CI).
#   ./scripts/deploy.sh dev            package + deploy + site to dev
#   ./scripts/deploy.sh prod --run-now and publish an edition right away
# Bootstrap (once per account): python scripts/deploy.py bootstrap --github-repo OWNER/REPO --email you@x.com
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_NAME="${1:-dev}"
shift || true
python3 scripts/deploy.py release --env "$ENV_NAME"
if [[ "${1:-}" == "--run-now" ]]; then
  python3 scripts/deploy.py run --env "$ENV_NAME"
fi
