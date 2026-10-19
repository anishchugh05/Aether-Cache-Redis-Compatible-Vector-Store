"""Lets AI tools launch the server as `python -m mnemolith serve`, which works even when the
`mnemolith` command isn't on their PATH (common for apps started from the dock or Start menu)."""
from .cli import main

main()
