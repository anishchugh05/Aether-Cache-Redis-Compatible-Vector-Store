# Mnemolith installer for Windows (PowerShell):
#
#   powershell -ExecutionPolicy ByPass -c "irm https://mnemolith.dev/install.ps1 | iex"
#
# 1. Installs uv (https://docs.astral.sh/uv/) if you don't have it. uv also takes care of Python.
# 2. Installs Mnemolith with uv, or upgrades it if it's already installed. If you had Aether Cache (its earlier
#    name), it's replaced, and setup moves your memories over.
# 3. Runs `mnemolith setup`, which starts the engine (downloading a private copy of Java if you
#    don't have one), downloads the embedding model, and connects Claude Code, Cursor, and Codex.
#
# Set $env:MNEMOLITH_SKIP_SETUP = "1" to install without running setup.
# Everything runs as your user: nothing needs administrator rights.

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @("$HOME\.local\bin\uv.exe", "$HOME\.cargo\bin\uv.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

$ContactUrl = "https://mnemolith.dev/install.html#contact"

function Fail($message) {
    Write-Host $message -ForegroundColor Red
    Write-Host "Still stuck? Tell us: $ContactUrl"
}

function Install-Mnemolith {
    Write-Host "Installing Mnemolith"
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
    $exe = Join-Path $binDir "mnemolith.exe"
    if (Test-Path $exe) {
        Write-Host "Mnemolith is already installed. Upgrading it to the latest version..."
        & $uv tool upgrade mnemolith
    } else {
        # Python 3.12, which uv downloads if needed: the newest Python releases can be ahead of the libraries
        # Mnemolith uses, and this keeps the install working whatever Python the computer already has.
        & $uv tool install --python 3.12 mnemolith
    }
    if ($LASTEXITCODE -ne 0) {
        Fail "Installing Mnemolith failed (see above)."
        return
    }
    & $uv tool update-shell *> $null  # so `mnemolith` works in new terminals
    Write-Host ""

    # Mnemolith used to be called Aether Cache. Replace the old install; setup moves its memories over.
    if (Test-Path (Join-Path $binDir "aether-cache.exe")) {
        Write-Host "Found Aether Cache, Mnemolith's earlier name. Removing it; your memories move over to Mnemolith next."
        & $uv tool uninstall aether-cache *> $null
        Write-Host ""
    }

    if ($env:MNEMOLITH_SKIP_SETUP) {
        Write-Host "Installed. Run 'mnemolith setup' when you're ready."
    } else {
        & $exe setup
        if ($LASTEXITCODE -ne 0) {
            Fail "Setup didn't finish. Fix the problem above, then run 'mnemolith setup'."
            return
        }
    }
    Write-Host ""
    Write-Host "Open a new terminal to use the mnemolith command."
}

Install-Mnemolith
