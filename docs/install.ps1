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

function Install-AetherCache {
    Write-Host "Installing Aether Cache"
    Write-Host ""

    $uv = Find-Uv
    if (-not $uv) {
        Write-Host "uv isn't installed, so installing it first (https://docs.astral.sh/uv/)..."
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $uv = Find-Uv
        if (-not $uv) {
            Write-Host "uv was installed but can't be found yet. Open a new terminal and run this again." -ForegroundColor Red
            return
        }
        Write-Host ""
    }

    $binDir = (& $uv tool dir --bin).Trim()
    $exe = Join-Path $binDir "aether-cache.exe"
    if (Test-Path $exe) {
        Write-Host "Aether Cache is already installed. Upgrading it to the latest version..."
        & $uv tool upgrade aether-cache
    } else {
        & $uv tool install aether-cache
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing Aether Cache failed (see above)." -ForegroundColor Red
        return
    }
    & $uv tool update-shell *> $null  # so `aether-cache` works in new terminals
    Write-Host ""

    if ($env:AETHER_SKIP_SETUP) {
        Write-Host "Installed. Run 'aether-cache setup' when you're ready."
    } else {
        & $exe setup
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Setup didn't finish. Fix the problem above, then run 'aether-cache setup'." -ForegroundColor Red
            return
        }
    }
    Write-Host ""
    Write-Host "Open a new terminal to use the aether-cache command."
}

Install-AetherCache
