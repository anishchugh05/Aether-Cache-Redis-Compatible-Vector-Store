"""Lets AI tools launch the server as `python -m aether_cache serve`, which works even when the
`aether-cache` command isn't on their PATH (common for apps started from the dock or Start menu)."""
from .cli import main

main()
