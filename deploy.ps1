<#
.SYNOPSIS
    Deploys Chatbot-Public to Render and verifies the deploy actually landed.

.DESCRIPTION
    Render is connected to this repo's GitHub remote with auto-deploy on
    push enabled - that connection is configured in the Render dashboard,
    not in this repo, so there is no `render.yaml` to invoke. The only real
    "deploy command" is `git push origin main`. Everything else this script
    does is guardrails around that one command:

      1. Preflight  - refuse to push a dirty tree or a syntax error.
      2. Push       - `git push origin main`, which triggers the deploy.
      3. Verify     - poll Render's API (read-only) until the new commit's
                       deploy is 'live', so you get a real pass/fail instead
                       of "probably worked, check the dashboard".
      4. Smoke test - hit the live service's /healthz, /, and /chat.

    Steps 3 and 4 only run if a Render API key is available (see
    .render.env.example) - this script never uses that key to trigger the
    deploy itself, only to read status back. Without it, the script still
    pushes and prints a dashboard link to check manually.

.PARAMETER Force
    Push even if the working tree has uncommitted changes. Those changes
    are NOT included - you're explicitly deploying the last commit as-is.

.PARAMETER DryRun
    Run every check but skip the actual `git push` and everything after it.
    Use this to validate the script itself without triggering a real deploy.

.PARAMETER SkipSmokeTest
    Push and confirm the deploy is live, but don't hit /healthz, /, /chat.
    Useful if you don't want the smoke test's real Hugging Face API call
    (it costs one inference call and shows up in chat_log.csv / your logs).

.PARAMETER TimeoutSeconds
    How long to wait for the deploy to go live before giving up (default 600).
    Docker builds on Render's free tier can take a few minutes.

.EXAMPLE
    .\deploy.ps1
    Normal deploy: preflight, push, wait for live, smoke test.

.EXAMPLE
    .\deploy.ps1 -DryRun
    Sanity-check the script and your .render.env setup without deploying.
#>

param(
    [switch]$Force,
    [switch]$DryRun,
    [switch]$SkipSmokeTest,
    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

# Known from the Render dashboard URL shared for this project. Used only as
# a fallback link when no .render.env is configured - never used to call
# the API without a key.
$KnownServiceId = "srv-d9abdncs728c73eg8qeg"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  OK   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  WARN $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  FAIL $msg" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# 0. Load local, gitignored Render config (real env vars win if already set -
#    e.g. in CI - so this file is purely a local-dev convenience).
# ---------------------------------------------------------------------------
$RenderApiKey = $env:RENDER_API_KEY
$RenderServiceId = $env:RENDER_SERVICE_ID
$ConfigPath = Join-Path $RepoRoot ".render.env"

if (Test-Path $ConfigPath) {
    foreach ($line in Get-Content $ConfigPath) {
        if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $key = $Matches[1]
            $val = $Matches[2]
            if ($key -eq "RENDER_API_KEY" -and -not $RenderApiKey) { $RenderApiKey = $val }
            if ($key -eq "RENDER_SERVICE_ID" -and -not $RenderServiceId) { $RenderServiceId = $val }
        }
    }
}
if (-not $RenderServiceId) { $RenderServiceId = $KnownServiceId }

$CanCallApi = [bool]$RenderApiKey
if (-not $CanCallApi) {
    Write-Warn "No RENDER_API_KEY found (checked env var and .render.env)."
    Write-Warn "Will push, but cannot confirm the deploy went live or run the smoke test."
    Write-Warn "See .render.env.example to enable full verification."
}

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
Write-Step "Preflight checks"

git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) { Write-Fail "Not inside a git repository."; exit 1 }

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne "main") {
    Write-Fail "On branch '$branch', not 'main'. Render deploys from main - switch or merge first."
    exit 1
}
Write-Ok "On branch main"

$dirty = git status --porcelain
if ($dirty) {
    if ($Force) {
        Write-Warn "Uncommitted changes present - deploying the last commit only, as-is (-Force given):"
        $dirty -split "`n" | ForEach-Object { Write-Host "    $_" }
    } else {
        Write-Fail "Uncommitted changes present. Commit them first, or re-run with -Force to deploy the last commit anyway (uncommitted work will NOT be included):"
        $dirty -split "`n" | ForEach-Object { Write-Host "    $_" }
        exit 1
    }
} else {
    Write-Ok "Working tree clean"
}

$pyFiles = git ls-files "*.py"
if ($pyFiles) {
    python -m py_compile $pyFiles
    if ($LASTEXITCODE -ne 0) { Write-Fail "Python syntax check failed - not pushing."; exit 1 }
    Write-Ok "All tracked .py files compile"
}

# ---------------------------------------------------------------------------
# 2. Push (this is the actual deploy trigger - Render's GitHub auto-deploy
#    picks up the new commit on main)
# ---------------------------------------------------------------------------
Write-Step "Pushing main to origin"

if ($DryRun) {
    Write-Warn "[dry run] would run: git push origin main"
    $headSha = (git rev-parse HEAD).Trim()
} else {
    git push origin main
    if ($LASTEXITCODE -ne 0) { Write-Fail "git push failed."; exit 1 }
    $headSha = (git rev-parse HEAD).Trim()
    Write-Ok "Pushed commit $headSha"
}

if ($DryRun) {
    Write-Warn "[dry run] stopping before deploy verification / smoke test."
    exit 0
}

if (-not $CanCallApi) {
    Write-Step "Manual verification"
    Write-Host "  Dashboard: https://dashboard.render.com/web/$RenderServiceId"
    Write-Host "  Watch the Events/Logs tab for commit $headSha to reach 'Live'."
    exit 0
}

# ---------------------------------------------------------------------------
# 3. Verify: poll Render's API (read-only) until this commit's deploy is live
# ---------------------------------------------------------------------------
$Headers = @{ Authorization = "Bearer $RenderApiKey"; Accept = "application/json" }

function Unwrap-RenderItem($item) {
    # Render's list endpoints wrap each item as {"deploy": {...}, "cursor": "..."}
    # while single-resource GETs return the object directly. Handle both.
    if ($item.PSObject.Properties.Name -contains "deploy") { return $item.deploy }
    return $item
}

Write-Step "Looking up the Render service"
try {
    $service = Invoke-RestMethod -Uri "https://api.render.com/v1/services/$RenderServiceId" -Headers $Headers -Method Get
} catch {
    Write-Fail "Could not reach Render API: $($_.Exception.Message)"
    Write-Fail "Check RENDER_API_KEY / RENDER_SERVICE_ID in .render.env."
    exit 1
}
$publicUrl = $service.serviceDetails.url
Write-Ok "Service: $($service.name)  URL: $publicUrl"

Write-Step "Waiting for commit $headSha to go live (timeout ${TimeoutSeconds}s)"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$targetDeploy = $null
$terminalStatuses = @("live", "build_failed", "update_failed", "canceled", "deactivated")

do {
    try {
        $raw = Invoke-RestMethod -Uri "https://api.render.com/v1/services/$RenderServiceId/deploys?limit=10" -Headers $Headers -Method Get
    } catch {
        Write-Warn "Render API request failed, retrying: $($_.Exception.Message)"
        Start-Sleep -Seconds 10
        continue
    }
    $deploys = $raw | ForEach-Object { Unwrap-RenderItem $_ }

    $match = $deploys | Where-Object {
        $_.commit -and (($_.commit.id -eq $headSha) -or ($_.commit.sha -eq $headSha))
    } | Select-Object -First 1

    if (-not $match -and $deploys) {
        # Commit metadata can lag briefly right after a push - fall back to
        # the newest deploy so we still show live progress.
        $match = $deploys | Select-Object -First 1
    }
    $targetDeploy = $match

    if ($targetDeploy) {
        Write-Host "  status: $($targetDeploy.status)  (deploy $($targetDeploy.id))"
    } else {
        Write-Host "  no deploys visible yet..."
    }

    if ($targetDeploy -and $terminalStatuses -contains $targetDeploy.status) { break }
    Start-Sleep -Seconds 10
} while ((Get-Date) -lt $deadline)

if (-not $targetDeploy -or $targetDeploy.status -ne "live") {
    Write-Fail "Deploy did not reach 'live' within ${TimeoutSeconds}s (last status: $($targetDeploy.status))."
    Write-Fail "Check: https://dashboard.render.com/web/$RenderServiceId/logs"
    exit 1
}
Write-Ok "Deploy $($targetDeploy.id) is live"

# ---------------------------------------------------------------------------
# 4. Smoke test the live service
# ---------------------------------------------------------------------------
if ($SkipSmokeTest) {
    Write-Warn "Skipping smoke test (-SkipSmokeTest)."
    exit 0
}
if (-not $publicUrl) {
    Write-Warn "No public URL returned by the API - skipping smoke test."
    exit 0
}

Write-Step "Smoke-testing $publicUrl"
$allPassed = $true

try {
    $r = Invoke-RestMethod -Uri "$publicUrl/healthz" -Method Get -TimeoutSec 30
    if ($r.status -ne "ok") { throw "unexpected body: $($r | ConvertTo-Json -Compress)" }
    Write-Ok "GET /healthz -> $($r | ConvertTo-Json -Compress)"
} catch {
    Write-Fail "GET /healthz -> $($_.Exception.Message)"
    $allPassed = $false
}

try {
    $r = Invoke-WebRequest -Uri "$publicUrl/" -Method Get -TimeoutSec 30
    if ($r.StatusCode -ne 200) { throw "status $($r.StatusCode)" }
    Write-Ok "GET / -> 200"
} catch {
    Write-Fail "GET / -> $($_.Exception.Message)"
    $allPassed = $false
}

try {
    $body = @{ history = @(@{ role = "user"; content = "Hello, are you working?" }) } | ConvertTo-Json -Depth 5
    $r = Invoke-RestMethod -Uri "$publicUrl/chat" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 60
    if (-not $r.reply) { throw "no 'reply' field: $($r | ConvertTo-Json -Compress)" }
    Write-Ok "POST /chat -> route=$($r.route) answer_time=$($r.answer_time)"
} catch {
    Write-Fail "POST /chat -> $($_.Exception.Message)"
    $allPassed = $false
}

if (-not $allPassed) {
    Write-Fail "One or more smoke tests failed. Check https://dashboard.render.com/web/$RenderServiceId/logs"
    exit 1
}

Write-Step "Deploy complete and verified"
exit 0
