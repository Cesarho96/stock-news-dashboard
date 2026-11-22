#!/usr/bin/env bash
# Deploy the AI stock news dashboard: CloudFormation stack (SAM) + static site upload.
#   ./scripts/deploy.sh --guided     first time (asks for parameters, saves samconfig.toml)
#   ./scripts/deploy.sh --run-now    deploy and publish an edition right away
#   ./scripts/deploy.sh --site-only  only re-upload web/
# Env: STACK_NAME (default stock-news-dashboard), AWS_REGION (default us-east-1), AWS_PROFILE.
set -euo pipefail

STACK_NAME="${STACK_NAME:-stock-news-dashboard}"
REGION="${AWS_REGION:-us-east-1}"
GUIDED=0; SITE_ONLY=0; RUN_NOW=0
for arg in "$@"; do
  case "$arg" in
    --guided) GUIDED=1 ;;
    --site-only) SITE_ONLY=1 ;;
    --run-now) RUN_NOW=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
for tool in aws sam; do
  command -v "$tool" >/dev/null || { echo "'$tool' CLI was not found in PATH" >&2; exit 1; }
done

if [[ $SITE_ONLY -eq 0 ]]; then
  echo "==> Deploying stack '$STACK_NAME' in $REGION"
  # No 'sam build' needed: the agent uses only the Python standard library and the runtime's boto3.
  if [[ $GUIDED -eq 1 ]]; then
    sam deploy --guided --template-file template.yaml --stack-name "$STACK_NAME" --region "$REGION"
  else
    sam deploy --template-file template.yaml --stack-name "$STACK_NAME" --region "$REGION" \
      --no-confirm-changeset --no-fail-on-empty-changeset
  fi
fi

output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
BUCKET="$(output SiteBucketName)"
URL="$(output DashboardUrl)"
FUNCTION="$(output NewsAgentFunctionName)"

echo "==> Uploading the site to s3://$BUCKET"
upload() {  # path key content-type cache-control
  aws s3 cp "$1" "s3://$BUCKET/$2" --content-type "$3" --cache-control "$4" --only-show-errors --region "$REGION"
}
upload web/index.html index.html "text/html; charset=utf-8" "no-cache"
upload web/assets/dashboard.js assets/dashboard.js "text/javascript; charset=utf-8" "public, max-age=300"
upload web/assets/dashboard.css assets/dashboard.css "text/css; charset=utf-8" "public, max-age=300"
upload web/assets/favicon.svg assets/favicon.svg "image/svg+xml" "public, max-age=86400"

if [[ $RUN_NOW -eq 1 ]]; then
  echo "==> Starting the news agent (the edition appears in about 2-5 minutes)"
  payload="$(mktemp)"; echo '{"source": "deploy"}' > "$payload"
  aws lambda invoke --function-name "$FUNCTION" --invocation-type Event --payload "fileb://$payload" \
    --region "$REGION" /dev/null >/dev/null
  rm -f "$payload"
fi

echo
echo "Dashboard: $URL"
