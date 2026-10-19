# Aether Cache installer for Windows (PowerShell):
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/anishchugh05/Aether-Cache-Redis-Compatible-Vector-Store/main/docs/install.ps1 | iex"
#
# 1. Installs uv (https://docs.astral.sh/uv/) if you don't have it. uv also takes care of Python.
# 2. Installs Aether Cache with uv, or upgrades it if it's already installed.
# 3. Runs `aether-cache setup`, which starts the engine (downloading a private copy of Java if you
#    don't have one), downloads the embedding model, and connects Claude Code, Cursor, and Codex.
#
# Set $env:AETHER_SKIP_SETUP = "1" to install without running setup.
# Everything runs as your user: nothing needs administrator rights.

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @("$HOME\.local\bin\uv.exe", "$HOME\.cargo\bin\uv.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

$ContactUrl = "https://anishchugh05.github.io/Aether-Cache-Redis-Compatible-Vector-Store/install.html#contact"

function Fail($message) {
    Write-Host $message -ForegroundColor Red
    Write-Host "Still stuck? Tell us: $ContactUrl"
}

function Install-AetherCache {
    Write-Host "Installing Aether Cache"
    Write-Host ""

    $uv = Find-Uv
    if (-not $uv) {
        Write-Host "uv isn't installed, so installing it first (from astral.sh, https://docs.astral.sh/uv/)..."
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:Path = "$HOME\.local\bin;$env:Path"  # this window doesn't see uv's folder until it's reopened
        $uv = Find-Uv
        if (-not $uv) {
            Fail "uv was installed but can't be found yet. Open a new terminal and run this again."
            return
        }
        Write-Host ""
    }

    $binDir = (& $uv tool dir --bin).Trim()
    $env:Path = "$binDir;$env:Path"  # so uv doesn't warn that the folder isn't on PATH yet; update-shell fixes it for good
    $exe = Join-Path $binDir "aether-cache.exe"
    if (Test-Path $exe) {
        Write-Host "Aether Cache is already installed. Upgrading it to the latest version..."
        & $uv tool upgrade aether-cache
    } else {
        # Python 3.12, which uv downloads if needed: the newest Python releases can be ahead of the libraries
        # Aether Cache uses, and this keeps the install working whatever Python the computer already has.
        & $uv tool install --python 3.12 aether-cache
    }
    if ($LASTEXITCODE -ne 0) {
        Fail "Installing Aether Cache failed (see above)."
        return
    }
    & $uv tool update-shell *> $null  # so `aether-cache` works in new terminals
    Write-Host ""

    if ($env:AETHER_SKIP_SETUP) {
        Write-Host "Installed. Run 'aether-cache setup' when you're ready."
    } else {
        & $exe setup
        if ($LASTEXITCODE -ne 0) {
            Fail "Setup didn't finish. Fix the problem above, then run 'aether-cache setup'."
            return
        }
    }
    Write-Host ""
    Write-Host "Open a new terminal to use the aether-cache command."
}

Install-AetherCache
