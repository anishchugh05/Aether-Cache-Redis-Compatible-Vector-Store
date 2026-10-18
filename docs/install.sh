#!/bin/sh
# Aether Cache installer for macOS and Linux:
#
#   curl -fsSL https://anishchugh05.github.io/Aether-Cache-Redis-Compatible-Vector-Store/install.sh | sh
#
# 1. Installs uv (https://docs.astral.sh/uv/) if you don't have it. uv also takes care of Python.
# 2. Installs Aether Cache with uv, or upgrades it if it's already installed.
# 3. Runs `aether-cache setup`, which starts the engine (downloading a private copy of Java if you
#    don't have one), downloads the embedding model, and connects Claude Code, Cursor, and Codex.
#
# Set AETHER_SKIP_SETUP=1 to install without running setup.
# Everything runs as your user: nothing needs sudo, and nothing is installed system-wide.
set -eu

say() { printf '%s\n' "$*"; }
fail() { printf '\nError: %s\nStill stuck? Tell us: https://anishchugh05.github.io/Aether-Cache-Redis-Compatible-Vector-Store/install.html#contact\n' "$*" >&2; exit 1; }

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  for candidate in "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then printf '%s\n' "$candidate"; return; fi
  done
}

main() {
  say "Installing Aether Cache"
  say ""

  uv=$(find_uv || true)
  if [ -z "$uv" ]; then
    command -v curl >/dev/null 2>&1 || fail "curl is needed to install uv. Install curl, then run this again."
    say "uv isn't installed, so installing it first (from astral.sh, https://docs.astral.sh/uv/)..."
    curl -LsSf "${AETHER_UV_INSTALLER:-https://astral.sh/uv/install.sh}" | sh
    uv=$(find_uv || true)
    [ -n "$uv" ] || fail "uv was installed but can't be found yet. Open a new terminal and run this again."
    say ""
  fi

  bin_dir=$("$uv" tool dir --bin)
  if [ -x "$bin_dir/aether-cache" ]; then
    say "Aether Cache is already installed. Upgrading it to the latest version..."
    "$uv" tool upgrade aether-cache || fail "Upgrading Aether Cache failed (see above)."
  else
    # Python 3.12, which uv downloads if needed: the newest Python releases can be ahead of the libraries
    # Aether Cache uses, and this keeps the install working whatever Python the computer already has.
    "$uv" tool install --python 3.12 aether-cache || fail "Installing Aether Cache failed (see above)."
  fi
  "$uv" tool update-shell >/dev/null 2>&1 || true  # so `aether-cache` works in new terminals
  say ""

  if [ -n "${AETHER_SKIP_SETUP:-}" ]; then
    say "Installed. Run \`aether-cache setup\` when you're ready."
  else
    "$bin_dir/aether-cache" setup </dev/null || fail "Setup didn't finish. Fix the problem above, then run \`aether-cache setup\`."
  fi
  say ""
  say "Open a new terminal to use the \`aether-cache\` command."
}

main "$@"
