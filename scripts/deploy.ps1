<#
.SYNOPSIS
  Deploy the AI stock news dashboard from Windows: a thin wrapper around scripts/deploy.py.

.DESCRIPTION
  The release pipeline (package once, deploy to dev and prod, smoke gate, rollback registry)
  lives in scripts/deploy.py so it behaves the same on Windows, Linux and in GitHub Actions.
  Needs Python 3.10+ with boto3 (pip install -r requirements-dev.txt) and the AWS SAM CLI.

.EXAMPLE
  .\scripts\deploy.ps1 -Bootstrap -GitHubRepo Cesarho96/stock-news-dashboard -Email you@example.com
.EXAMPLE
  .\scripts\deploy.ps1 -Env dev -RunNow      # package + deploy + site, then publish an edition
.EXAMPLE
  .\scripts\deploy.ps1 -Env prod -AwsProfile personal
#>
[CmdletBinding()]
param(
  [ValidateSet("dev", "prod")][string]$Env = "dev",
  [string]$Region = "us-east-1",
  [string]$AwsProfile = "",
  [switch]$Bootstrap,
  [string]$GitHubRepo = "",
  [string]$Email = "",
  [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Common = @("--region", $Region)
if ($AwsProfile) { $Common += @("--profile", $AwsProfile) }

Push-Location $Root
try {
  if ($Bootstrap) {
    if (-not $GitHubRepo) { throw "-GitHubRepo OWNER/REPO is required with -Bootstrap" }
    $BootArgs = @("--github-repo", $GitHubRepo)
    if ($Email) { $BootArgs += @("--email", $Email) }
    python scripts/deploy.py @Common bootstrap @BootArgs
  } else {
    python scripts/deploy.py @Common release --env $Env
    if ($LASTEXITCODE -ne 0) { throw "deploy failed" }
    if ($RunNow) { python scripts/deploy.py @Common run --env $Env }
  }
  if ($LASTEXITCODE -ne 0) { throw "deploy.py exited with code $LASTEXITCODE" }
} finally {
  Pop-Location
}
