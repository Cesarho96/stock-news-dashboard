<#
.SYNOPSIS
  Deploy the AI stock news dashboard to AWS: CloudFormation stack (SAM) + static site upload.

.EXAMPLE
  .\scripts\deploy.ps1 -Guided     # first time: asks for parameters and saves them to samconfig.toml
.EXAMPLE
  .\scripts\deploy.ps1 -RunNow     # later deploys; -RunNow also publishes an edition right away
.EXAMPLE
  .\scripts\deploy.ps1 -SiteOnly   # only re-upload web/ after editing the CSS/JS
.EXAMPLE
  .\scripts\deploy.ps1 -AwsProfile personal -Region us-east-1
#>
[CmdletBinding()]
param(
  [string]$StackName = "stock-news-dashboard",
  [string]$Region = "us-east-1",
  [string]$AwsProfile = "",
  [switch]$Guided,
  [switch]$SiteOnly,
  [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
  foreach ($tool in @("aws", "sam")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "'$tool' CLI was not found in PATH." }
  }
  $awsArgs = @("--region", $Region)
  if ($AwsProfile) { $awsArgs += @("--profile", $AwsProfile) }

  if (-not $SiteOnly) {
    Write-Host "==> Deploying stack '$StackName' in $Region" -ForegroundColor Cyan
    # No 'sam build' needed: the agent uses only the Python standard library and the runtime's boto3.
    $samArgs = @("deploy", "--template-file", "template.yaml", "--stack-name", $StackName) + $awsArgs
    if ($Guided) { $samArgs += "--guided" } else { $samArgs += @("--no-confirm-changeset", "--no-fail-on-empty-changeset") }
    & sam @samArgs
    if ($LASTEXITCODE -ne 0) { throw "sam deploy failed (exit code $LASTEXITCODE)." }
  }

  $json = & aws cloudformation describe-stacks --stack-name $StackName @awsArgs --query "Stacks[0].Outputs" --output json
  if ($LASTEXITCODE -ne 0) { throw "Could not read the stack outputs." }
  $out = @{}
  foreach ($o in ($json | Out-String | ConvertFrom-Json)) { $out[$o.OutputKey] = $o.OutputValue }
  $bucket = $out["SiteBucketName"]

  Write-Host "==> Uploading the site to s3://$bucket" -ForegroundColor Cyan
  # Explicit content types: Windows can map .js to text/plain, which browsers refuse to run.
  $files = @(
    @{ Path = "web/index.html"; Key = "index.html"; Type = "text/html; charset=utf-8"; Cache = "no-cache" },
    @{ Path = "web/assets/dashboard.js"; Key = "assets/dashboard.js"; Type = "text/javascript; charset=utf-8"; Cache = "public, max-age=300" },
    @{ Path = "web/assets/dashboard.css"; Key = "assets/dashboard.css"; Type = "text/css; charset=utf-8"; Cache = "public, max-age=300" },
    @{ Path = "web/assets/favicon.svg"; Key = "assets/favicon.svg"; Type = "image/svg+xml"; Cache = "public, max-age=86400" }
  )
  foreach ($f in $files) {
    & aws s3 cp $f.Path "s3://$bucket/$($f.Key)" --content-type $f.Type --cache-control $f.Cache --only-show-errors @awsArgs
    if ($LASTEXITCODE -ne 0) { throw "Upload failed: $($f.Path)" }
  }

  if ($RunNow) {
    Write-Host "==> Starting the news agent (the edition appears in about 2-5 minutes)" -ForegroundColor Cyan
    $payload = Join-Path ([IO.Path]::GetTempPath()) "stocknews-payload.json"
    $result = Join-Path ([IO.Path]::GetTempPath()) "stocknews-invoke.json"
    Set-Content -Path $payload -Value '{"source": "deploy"}' -Encoding ascii
    & aws lambda invoke --function-name $out["NewsAgentFunctionName"] --invocation-type Event `
      --payload "fileb://$payload" $result @awsArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not invoke the news agent." }
  }

  Write-Host ""
  Write-Host "Dashboard: $($out['DashboardUrl'])" -ForegroundColor Green
}
finally {
  Pop-Location
}
