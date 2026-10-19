#!/bin/sh
# Mnemolith installer for macOS and Linux:
#
#   curl -fsSL https://mnemolith.dev/install.sh | sh
#
# 1. Installs uv (https://docs.astral.sh/uv/) if you don't have it. uv also takes care of Python.
# 2. Installs Mnemolith with uv, or upgrades it if it's already installed. If you had Aether Cache (its earlier
#    name), it's replaced, and setup moves your memories over.
# 3. Runs `mnemolith setup`, which starts the engine (downloading a private copy of Java if you
#    don't have one), downloads the embedding model, and connects Claude Code, Cursor, and Codex.
#
# Set MNEMOLITH_SKIP_SETUP=1 to install without running setup.
# Everything runs as your user: nothing needs sudo, and nothing is installed system-wide.
set -eu

say() { printf '%s\n' "$*"; }
fail() { printf '\nError: %s\nStill stuck? Tell us: https://mnemolith.dev/install.html#contact\n' "$*" >&2; exit 1; }

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  for candidate in "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then printf '%s\n' "$candidate"; return; fi
  done
}

main() {
  say "Installing Mnemolith"
  say ""

  uv=$(find_uv || true)
  if [ -z "$uv" ]; then
    command -v curl >/dev/null 2>&1 || fail "curl is needed to install uv. Install curl, then run this again."
    say "uv isn't installed, so installing it first (from astral.sh, https://docs.astral.sh/uv/)..."
    curl -LsSf "${MNEMOLITH_UV_INSTALLER:-https://astral.sh/uv/install.sh}" | sh
    uv=$(find_uv || true)
    [ -n "$uv" ] || fail "uv was installed but can't be found yet. Open a new terminal and run this again."
    say ""
  fi

  bin_dir=$("$uv" tool dir --bin)
  PATH="$bin_dir:$PATH"; export PATH  # so uv doesn't warn that the folder isn't on PATH yet; update-shell fixes it for good
  if [ -x "$bin_dir/mnemolith" ]; then
    say "Mnemolith is already installed. Upgrading it to the latest version..."
    "$uv" tool upgrade mnemolith || fail "Upgrading Mnemolith failed (see above)."
  else
    # Python 3.12, which uv downloads if needed: the newest Python releases can be ahead of the libraries
    # Mnemolith uses, and this keeps the install working whatever Python the computer already has.
    "$uv" tool install --python 3.12 mnemolith || fail "Installing Mnemolith failed (see above)."
  fi
  "$uv" tool update-shell >/dev/null 2>&1 || true  # so `mnemolith` works in new terminals
  say ""

  # Mnemolith used to be called Aether Cache. Replace the old install; setup moves its memories over.
  if [ -x "$bin_dir/aether-cache" ]; then
    say "Found Aether Cache, Mnemolith's earlier name. Removing it; your memories move over to Mnemolith next."
    "$uv" tool uninstall aether-cache >/dev/null 2>&1 || true
    say ""
  fi

  if [ -n "${MNEMOLITH_SKIP_SETUP:-}" ]; then
    say "Installed. Run \`mnemolith setup\` when you're ready."
  else
    "$bin_dir/mnemolith" setup </dev/null || fail "Setup didn't finish. Fix the problem above, then run \`mnemolith setup\`."
  fi
  say ""
  say "Open a new terminal to use the \`mnemolith\` command."
}

main "$@"
