"""The `aether-cache` command."""
import argparse
import sys
from collections import Counter

from . import __version__, config, connect, engine

OK, WARN, FAIL = "  ok  ", "  !!  ", " FAIL "


def _say(mark: str, text: str) -> None:
    lines = text.splitlines() or [""]
    print(f"[{mark}] {lines[0]}")
    for line in lines[1:]:
        print(f"         {line}")


# ---------- commands ----------

def cmd_setup(args) -> int:
    print(f"Setting up Aether Cache {__version__}\n")
    try:
        java, major = engine.find_java()
        _say(OK, f"Java {major} ({java})")
        _say(OK, engine.start())
    except engine.EngineError as e:
        _say(FAIL, str(e))
        print("\nFix that, then run `aether-cache setup` again.")
        return 1

    if config.FAKE_EMBEDDER:
        _say(OK, "Embedding model: test mode, nothing to download")
    else:
        print("[ .... ] Downloading the embedding model (about 90 MB, only the first time)...", flush=True)
        try:
            from .server import embed
            embed("warm up")
            _say(OK, f"Embedding model ready ({config.MODEL})")
        except Exception as e:
            _say(WARN, f"Couldn't download the embedding model yet: {e}\n"
                       f"It will be downloaded the first time your AI saves or searches a memory.")

    tools = args.tools or connect.detected()
    if not tools:
        _say(WARN, "No supported AI coding tools found (Claude Code, Cursor, Codex).")
        print("\n" + connect.generic_instructions())
    failed = []
    for tool in tools:
        done, message = connect.CONNECTORS[tool]()
        _say(OK if done else FAIL, message)
        if not done:
            failed.append(tool)
    if failed:
        print(f"\nAlmost done: fix the {', '.join(failed)} problem above, then run "
              f"`aether-cache connect {' '.join(failed)}`.")
        return 1

    print("\nDone. Open your AI tool in any git repo and try:\n"
          '  "Remember that our tests need DOCKER_HOST set. It depends on docker-compose.yml."\n'
          "Then, in a new session:\n"
          '  "How do I run the tests?"\n'
          "\nSee and fix what your AI remembers any time with `aether-cache ui`.")
    return 0


def cmd_start(args) -> int:
    try:
        print(engine.start())
        return 0
    except engine.EngineError as e:
        print(e)
        return 1


def cmd_stop(args) -> int:
    print(engine.stop())
    return 0


def cmd_status(args) -> int:
    state = engine.probe()
    if state == "aether":
        pid = engine.running_pid()
        print(f"Engine: running on {config.HOST}:{config.PORT}" + (f" (pid {pid})" if pid else ""))
    elif state == "other":
        print(f"Engine: not running. {engine.port_taken_message()}")
        return 1
    else:
        print("Engine: not running (it starts automatically when your AI tool needs it, or run `aether-cache start`)")
    print(f"Data:   {config.DATA_DIR}")
    if state == "aether":
        from .server import project_name, run
        project = project_name()
        counts = Counter(status for _, status, _, _ in run("MEM.LIST", project, "STATUS", "any"))
        summary = ", ".join(f"{counts[s]} {s}" for s in ("active", "stale", "superseded") if counts[s])
        print(f"This project ({project}): {summary or 'no memories yet'}")
    connected = [t for t in connect.TOOLS if connect.CHECKS[t]()]
    print("Connected tools: " + (", ".join(connected) if connected else "none (run `aether-cache connect`)"))
    return 0


def cmd_connect(args) -> int:
    if args.print:
        print(connect.generic_instructions())
        return 0
    tools = args.tools or connect.detected()
    if not tools:
        print("No supported AI coding tools found (Claude Code, Cursor, Codex).\n")
        print(connect.generic_instructions())
        return 1
    failed = False
    for tool in tools:
        done, message = connect.CONNECTORS[tool]()
        _say(OK if done else FAIL, message)
        failed |= not done
    return 1 if failed else 0


def cmd_doctor(args) -> int:
    problems = 0
    _say(OK, f"Aether Cache {__version__}, Python {sys.version.split()[0]} ({sys.executable})")
    try:
        java, major = engine.find_java()
        _say(OK, f"Java {major} ({java})")
    except engine.EngineError as e:
        _say(FAIL, str(e))
        problems += 1
    if engine.JAR.is_file():
        _say(OK, f"Engine file present ({engine.JAR.name})")
    else:
        _say(FAIL, f"Engine file missing ({engine.JAR}). Reinstall Aether Cache.")
        problems += 1
    state = engine.probe()
    if state == "aether":
        _say(OK, f"Engine answering on {config.HOST}:{config.PORT}")
    elif state == "other":
        _say(FAIL, engine.port_taken_message())
        problems += 1
    else:
        _say(WARN, f"Engine not running. It starts automatically when needed; `aether-cache start` starts it now.\n"
                   f"If it won't start, see {config.LOG_FILE}")
    if config.FAKE_EMBEDDER:
        _say(WARN, "Test embedder is on (AETHER_EMBEDDER=fake): search quality is poor. Unset it for real use.")
    elif any(config.MODELS_DIR.glob("*")):
        _say(OK, f"Embedding model downloaded ({config.MODELS_DIR})")
    else:
        _say(WARN, "Embedding model not downloaded yet; it downloads on first use (about 90 MB).")
    for tool in connect.TOOLS:
        if connect.CHECKS[tool]():
            _say(OK, f"Connected to {tool}")
        elif tool in connect.detected():
            _say(WARN, f"{tool} is installed but not connected. Run `aether-cache connect {tool}`.")
    from .server import git_toplevel, project_name
    top = git_toplevel()
    _say(OK if top else WARN, f"This folder's project: {project_name()}"
         + ("" if top else " (not a git repo, so memories can't be linked to file versions here)"))
    print("\nEverything looks good." if problems == 0 else f"\n{problems} problem(s) to fix above.")
    return 1 if problems else 0


def cmd_list(args) -> int:
    from .server import EngineDown, project_name, run, store_down
    try:
        rows = run("MEM.LIST", project_name(), "STATUS", args.status)
    except EngineDown as e:
        print(store_down(e))
        return 1
    if not rows:
        print(f"No {'' if args.status == 'any' else args.status + ' '}memories in {project_name()} yet.")
    for mid, status, files, text in rows:
        linked = ", ".join(f.rsplit("@", 1)[0] for f in files.split(",") if f)
        print(f"{mid:>5}  {status:<10}  {text}" + (f"   [{linked}]" if linked else ""))
    return 0


def _tool_output(name: str, *tool_args) -> int:
    from . import server
    print(getattr(server, name)(*tool_args))
    return 0


def cmd_add(args) -> int:
    from . import repos
    from .server import EngineDown, store_down
    try:
        engine.ensure_running()
        added = repos.add(args.path)
    except repos.RepoError as e:
        print(e)
        return 1
    except (engine.EngineError, EngineDown) as e:
        print(store_down(e))
        return 1
    print(f"Added {added['name']} ({added['path']})" + (f", github.com/{added['github']}" if added["github"] else "") + ".")
    return 0


def _ask(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_import(args) -> int:
    from pathlib import Path
    from . import portability, repos, server
    folder = Path(args.path).expanduser()
    if not folder.is_dir():
        print(f"{args.path} isn't a folder.")
        return 1
    root = repos.git_top(folder) or folder.resolve()
    project = root.name
    try:
        engine.ensure_running()
        found = portability.scan(project, root)
    except (engine.EngineError, server.EngineDown) as e:
        print(server.store_down(e))
        return 1
    if not found["files"]:
        print(f"No notes files in {root}.\nAether Cache looks for: " + ", ".join(portability.NOTE_FILES)
              + ", and .cursor/rules/.")
        return 0
    notes = found["notes"]
    new = [n for n in notes if n["selected"]]
    missing = [n for n in notes if not n["known"] and n["missing"]]
    known = [n for n in notes if n["known"]]
    print(f"Found {len(notes)} notes in " + ", ".join(f["path"] for f in found["files"]) + f" ({project})\n")
    for i, n in enumerate(new, 1):
        print(f"  {i:>3}. {n['text']}" + (f"\n       linked to {', '.join(n['files'])}" if n["files"] else ""))
    if missing:
        print(f"\nLeft out: {len(missing)} that {'mentions a file' if len(missing) == 1 else 'mention files'} "
              f"that no longer {'exists' if len(missing) == 1 else 'exist'} (probably out of date):")
        for n in missing:
            print(f"     - {n['text']}\n       {', '.join(n['missing'])} not found")
    if known:
        print(f"\nAlready remembered: {len(known)}")
    if not new:
        print("\nNothing new to import.")
        return 0
    if missing:
        print("\nTo pick notes one by one, including the left-out ones, use Import in `aether-cache ui`.")
    if not args.yes and not _ask(f"\nImport {len(new)} {'memory' if len(new) == 1 else 'memories'} into {project}?"):
        print("Nothing imported." + ("" if sys.stdin.isatty() else " Pass --yes to import without asking."))
        return 0
    try:
        result = portability.apply(project, root)
        repos.add(str(root))  # so the dashboard can check the imported memories' files
    except repos.RepoError:
        pass
    except server.EngineDown as e:
        print(server.store_down(e))
        return 1
    saved = len(result["saved"])
    print(f"Imported {saved} {'memory' if saved == 1 else 'memories'} into {project}."
          + (f" {result['duplicates']} matched memories you already had." if result["duplicates"] else "")
          + (f"\n{result['conflicts']} may contradict an existing memory: decide in `aether-cache ui`."
             if result["conflicts"] else ""))
    return 0


def cmd_export(args) -> int:
    import json
    from pathlib import Path
    from . import portability, server
    try:
        engine.ensure_running()
        data = portability.export(args.project or None)
    except (engine.EngineError, server.EngineDown) as e:
        print(server.store_down(e))
        return 1
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
        return 0
    out = Path(args.output or portability.export_filename(args.project[0] if len(args.project or []) == 1 else None))
    out = out.expanduser()
    out.write_text(text, encoding="utf-8")
    n = sum(len(p["memories"]) for p in data["projects"])
    print(f"Exported {n} {'memory' if n == 1 else 'memories'} from {len(data['projects'])} "
          f"{'project' if len(data['projects']) == 1 else 'projects'} to {out.resolve()}")
    return 0


def _uninstall_command() -> str:
    exe = sys.executable.replace("\\", "/")
    if "/uv/tools/" in exe or "/uv/tool/" in exe:
        return "uv tool uninstall aether-cache"
    if "/pipx/" in exe:
        return "pipx uninstall aether-cache"
    return f"{sys.executable} -m pip uninstall aether-cache"


def cmd_uninstall(args) -> int:
    import shutil
    print("Uninstalling Aether Cache\n")
    failed = False
    for tool in connect.TOOLS:
        done, message = connect.DISCONNECTORS[tool]()
        _say(OK if done else FAIL, message)
        failed |= not done
    print("         Restart any AI tools that are open, so they stop using Aether Cache.")
    count = None
    if engine.probe() == "aether":
        try:
            from .ui import all_memories
            count = len(all_memories())
        except Exception:
            pass
    stopped = engine.stop()
    still_running = engine.probe() == "aether"
    _say(FAIL if still_running else OK, stopped)
    failed |= still_running

    has_data = config.HOME.exists()
    if still_running and has_data:  # it would rewrite them anyway
        print(f"\nYour memories are kept in {config.HOME}: stop the engine first to delete them.")
        print(f"\nLast step, remove the program itself:\n  {_uninstall_command()}")
        return 1
    delete = args.delete_data
    if has_data and not delete:
        what = f"Your {count} {'memory is' if count == 1 else 'memories are'}" if count else "Your memories are"
        print(f"\n{what} in {config.HOME}.")
        delete = _ask("Delete them too? This can't be undone (keep a copy first with `aether-cache export`).")
    if has_data and delete:
        shutil.rmtree(config.HOME, ignore_errors=True)
        _say(OK, f"Deleted {config.HOME}")
    elif has_data:
        _say(OK, f"Kept your memories in {config.HOME}. Reinstall any time and they're back.")
    print(f"\nLast step, remove the program itself:\n  {_uninstall_command()}")
    return 1 if failed else 0


def cmd_serve(args) -> int:
    from .server import serve
    serve()
    return 0


def main(argv=None) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")  # never crash printing on a limited console
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        prog="aether-cache",
        description="Shared memory for AI coding tools that stays true to your code.")
    parser.add_argument("--version", action="version", version=f"aether-cache {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("setup", help="one-time setup: start the engine, download the model, connect your AI tools")
    p.add_argument("tools", nargs="*", metavar="tool",
                   help="which tools to connect (default: every supported tool found)")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("connect", help="connect AI tools: claude-code, cursor, codex")
    p.add_argument("tools", nargs="*", metavar="tool",
                   help="which tools to connect (default: every supported tool found)")
    p.add_argument("--print", action="store_true", help="print config for any other MCP tool instead")
    p.set_defaults(func=cmd_connect)
    sub.add_parser("start", help="start the engine in the background").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the engine").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="is it running, and what's in this project's memory").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="check everything and say how to fix problems").set_defaults(func=cmd_doctor)
    p = sub.add_parser("list", help="list this project's memories")
    p.add_argument("--status", default="active", choices=["active", "stale", "superseded", "any"])
    p.set_defaults(func=cmd_list)
    sub.add_parser("recall", help="search this project's memories").add_argument("query")
    sub.choices["recall"].set_defaults(func=lambda a: _tool_output("recall", a.query))
    sub.add_parser("explain", help="where a memory came from and its history").add_argument("id")
    sub.choices["explain"].set_defaults(func=lambda a: _tool_output("explain_memory", a.id))
    sub.add_parser("sync", help="write current memories into AGENTS.md / CLAUDE.md").set_defaults(
        func=lambda a: _tool_output("sync_to_files"))
    sub.add_parser("check", help="retire memories whose linked files changed").set_defaults(
        func=lambda a: _tool_output("check_stale"))
    p = sub.add_parser("add", help="add a repo so the dashboard can check its files (default: this folder)")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("import", help="turn notes in CLAUDE.md, AGENTS.md, and Cursor rules into memories")
    p.add_argument("path", nargs="?", default=".", help="the repo to import from (default: this folder)")
    p.add_argument("--yes", "-y", action="store_true", help="import without asking")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("export", help="save memories, with their history, to a JSON file")
    p.add_argument("--project", "-p", action="append", metavar="NAME", help="only this project (repeatable; default: all)")
    p.add_argument("--output", "-o", metavar="FILE", help="where to write it (default: aether-cache-<date>.json here; - for stdout)")
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("uninstall", help="disconnect your AI tools, stop the engine, and optionally delete memories")
    p.add_argument("--delete-data", action="store_true", help="also delete all memories, without asking")
    p.set_defaults(func=cmd_uninstall)
    p = sub.add_parser("ui", help="open the dashboard: see and fix what your AI tools remember")
    p.add_argument("--port", type=int, help="port for the dashboard (default 7381)")
    p.add_argument("--no-open", action="store_true", help="don't open a browser, just print the link")
    p.set_defaults(func=lambda a: __import__("aether_cache.ui", fromlist=["serve"]).serve(a.port, not a.no_open))
    sub.add_parser("serve", help="run the MCP server (your AI tool runs this for you)").set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    unknown = [t for t in getattr(args, "tools", None) or [] if t not in connect.TOOLS]
    if unknown:
        parser.error(f"unknown tool {unknown[0]!r}; choose from {', '.join(connect.TOOLS)} "
                     f"(or `aether-cache connect --print` for any other MCP tool)")
    if not getattr(args, "func", None):
        parser.print_help()
        print("\nNew here? Run `aether-cache setup`.")
        sys.exit(0)
    try:
        sys.exit(args.func(args))
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:  # output piped into something like `head` that stopped reading
        sys.stderr.close()
        sys.exit(0)
