"""Aether Cache is now Mnemolith. This last release points people to it, and keeps AI tools that still
launch `aether_cache` working by running Mnemolith's server underneath."""
import sys

__version__ = "0.2.0"

NOTICE = """
Aether Cache is now Mnemolith. Same tool, same memories, new name.

Move over with one command. It installs Mnemolith, removes aether-cache, moves your memories,
and reconnects your AI tools:

  macOS / Linux:  curl -fsSL https://mnemolith.dev/install.sh | sh
  Windows:        powershell -ExecutionPolicy ByPass -c "irm https://mnemolith.dev/install.ps1 | iex"

Then use the `mnemolith` command (for example `mnemolith ui`). More: https://mnemolith.dev
"""


def main(argv=None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["serve"]:
        # An AI tool connected under the old name: keep it working. stdout carries the MCP protocol,
        # so the notice goes to stderr, which the tool shows in its logs.
        print(NOTICE, file=sys.stderr, flush=True)
        from mnemolith.cli import main as mnemolith_main
        mnemolith_main(["serve"])
        return
    if args[:1] == ["--version"]:
        print(f"aether-cache {__version__} (now Mnemolith)")
        return
    print(NOTICE)
