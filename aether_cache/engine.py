"""Finding Java, and starting, stopping and checking the Java engine in the background."""
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Tuple

import redis

from . import config

JAR = Path(__file__).resolve().parent / "engine" / "aether-engine.jar"
MIN_JAVA = 11


class EngineError(Exception):
    """Something the user has to fix before the engine can run; the message says what."""


# ---------- Java ----------

def java_install_hint() -> str:
    system = platform.system()
    if system == "Darwin":
        how = "brew install openjdk@21   (or download it from https://adoptium.net)"
    elif system == "Windows":
        how = "winget install EclipseAdoptium.Temurin.21.JRE   (or download it from https://adoptium.net)"
    else:
        how = "sudo apt install openjdk-21-jre-headless   (or your distro's equivalent, or https://adoptium.net)"
    return (f"Run `aether-cache setup` and it downloads a private copy of Java for you, "
            f"or install Java {MIN_JAVA}+ yourself:\n    {how}")


def _java_version(java: str) -> Optional[int]:
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'version "(\d+)(?:\.(\d+))?', out.stderr + out.stdout)
    if out.returncode != 0 or not match:
        return None
    major = int(match.group(1))
    if major == 1 and match.group(2):  # old-style "1.8.0" means Java 8
        major = int(match.group(2))
    return major


def find_java() -> Tuple[str, int]:
    """(path to a working java, its major version). Raises EngineError if there's no usable Java.
    Prefers the computer's own Java; falls back to the private copy `setup` downloads when there isn't one.
    Runs `java -version` rather than trusting the PATH: macOS ships a /usr/bin/java stub that only
    says "Unable to locate a Java Runtime"."""
    from .javart import private_java
    candidates = []
    if not os.environ.get("AETHER_IGNORE_SYSTEM_JAVA"):  # testing only: act as if this computer had no Java
        if os.environ.get("JAVA_HOME"):
            candidates.append(str(Path(os.environ["JAVA_HOME"]) / "bin" / ("java.exe" if os.name == "nt" else "java")))
        if shutil.which("java"):
            candidates.append(shutil.which("java"))
    private = private_java()
    if private:
        candidates.append(str(private))
    too_old = None
    for java in candidates:
        major = _java_version(java)
        if major is None:
            continue
        if major >= MIN_JAVA:
            return java, major
        too_old = major
    if too_old:
        raise EngineError(f"Found Java {too_old}, which is too old.\n" + java_install_hint())
    raise EngineError("Java isn't installed.\n" + java_install_hint())


def is_private(java: str) -> bool:
    from .javart import JAVA_DIR
    try:
        return Path(java).resolve().is_relative_to(JAVA_DIR.resolve())
    except (OSError, ValueError):
        return False


# ---------- talking to the engine ----------

def client(timeout: Optional[float] = None) -> redis.Redis:
    # protocol=2 and no lib_name/lib_version: the engine speaks RESP2 and doesn't implement
    # HELLO or CLIENT SETINFO, which newer redis-py clients send by default
    with warnings.catch_warnings():  # newer redis-py warns that lib_name is deprecated; harmless here
        warnings.simplefilter("ignore")
        return redis.Redis(host=config.HOST, port=config.PORT, protocol=2, decode_responses=True,
                           lib_name=None, lib_version=None,
                           socket_connect_timeout=timeout, socket_timeout=timeout)


def probe() -> Optional[str]:
    """"aether" if our engine answers on the port, "other" if some other program does, None if nothing does."""
    c = client(timeout=2)
    try:
        c.ping()
    except (redis.ConnectionError, redis.TimeoutError, OSError):
        return None
    except redis.ResponseError:
        return "other"
    try:
        c.execute_command("MEM.LIST", "__aether_probe__")  # only Aether Cache knows this command
        return "aether"
    except redis.ResponseError:
        return "other"
    except (redis.ConnectionError, redis.TimeoutError, OSError):
        return None
    finally:
        c.close()


def port_taken_message() -> str:
    return (f"Port {config.PORT} is already used by another program (maybe a real Redis). "
            f"Pick a different port for Aether Cache: set AETHER_PORT (e.g. export AETHER_PORT={config.PORT + 10}), "
            f"then run `aether-cache setup` again so your AI tools use it too.")


# ---------- starting and stopping ----------

@contextmanager
def _start_lock():
    """Several AI sessions can start at once and all try to launch the engine; only one may."""
    config.HOME.mkdir(parents=True, exist_ok=True)
    with open(config.HOME / "start.lock", "a+") as f:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)


def _migrate_old_data() -> Optional[str]:
    """Earlier versions (called memstore) kept data in ~/.memstore. Copy it over once, leaving the original."""
    old = Path.home() / ".memstore"
    if old.is_dir() and not config.DATA_DIR.exists() and not os.environ.get("AETHER_HOME"):
        shutil.copytree(old, config.DATA_DIR)
        return f"Copied your existing memories from {old} to {config.DATA_DIR}."
    return None


def _read_pid() -> Optional[int]:
    try:
        return int(config.PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _log_tail(lines: int = 15) -> str:
    try:
        return "\n".join(config.LOG_FILE.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


_fingerprint = None


def jar_fingerprint() -> str:
    """Identifies this version of the engine file, so a running engine from an older version can be spotted."""
    global _fingerprint
    if _fingerprint is None:
        import hashlib
        _fingerprint = hashlib.sha256(JAR.read_bytes()).hexdigest()[:16] if JAR.is_file() else ""
    return _fingerprint


def needs_upgrade() -> bool:
    """True if the engine we started earlier came from a different engine file than this install's
    (e.g. after `uv tool upgrade aether-cache`). Engines we didn't start are left alone."""
    if running_pid() is None:
        return False
    try:
        stamp = config.STAMP_FILE.read_text().strip()
    except OSError:
        stamp = ""  # started by a version from before stamps existed
    return stamp != jar_fingerprint()


def start(wait: float = 60) -> str:
    """Starts the engine in the background (if it isn't already running) and waits until it answers.
    If the running engine is from an older install, restarts it on the new one (no data is lost:
    every acknowledged write is already on disk)."""
    state = probe()
    if state == "aether" and not needs_upgrade():
        return f"Engine already running on {config.HOST}:{config.PORT}."
    if state == "other":
        raise EngineError(port_taken_message())
    if config.HOST not in ("127.0.0.1", "localhost", "::1"):
        raise EngineError(f"The engine at {config.HOST}:{config.PORT} isn't reachable, and it's on another "
                          f"machine, so it can't be started from here.")
    if not JAR.is_file():
        raise EngineError(f"The engine file is missing ({JAR}). Reinstall Aether Cache.")

    upgraded = False
    with _start_lock():
        state = probe()  # another session may have started it while we waited for the lock
        if state == "aether":
            if not needs_upgrade():
                return f"Engine already running on {config.HOST}:{config.PORT}."
            stop()
            upgraded = True
        if state == "other":
            raise EngineError(port_taken_message())
        java, _ = find_java()
        note = _migrate_old_data()
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        command = [java, "-Xmx1g", "-jar", str(JAR),
                   "--port", str(config.PORT), "--bind", "127.0.0.1",
                   "--dir", str(config.DATA_DIR), "--appendonly", "yes",
                   # every memory write reaches the disk before it's acknowledged; memory writes are rare,
                   # so the cost doesn't matter, and nothing is ever lost
                   "--appendfsync", "always"]
        detach = {}
        if os.name == "nt":
            detach["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
        else:
            detach["start_new_session"] = True  # keeps running after the terminal or AI session closes
        with open(config.LOG_FILE, "ab") as log:
            log.write(f"\n--- starting engine {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
            log.flush()
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       cwd=str(config.HOME), **detach)
        config.PID_FILE.write_text(str(process.pid))

        deadline = time.time() + wait
        while time.time() < deadline:
            if process.poll() is not None:
                raise EngineError(f"The engine stopped right after starting. Last lines of {config.LOG_FILE}:\n"
                                  + _log_tail())
            state = probe()
            if state == "aether":
                config.STAMP_FILE.write_text(jar_fingerprint())
                started = (f"Engine {'updated and restarted' if upgraded else 'started'} on {config.HOST}:{config.PORT} "
                           f"(data in {config.DATA_DIR}).")
                return f"{note}\n{started}" if note else started
            if state == "other":
                process.terminate()
                raise EngineError(port_taken_message())
            time.sleep(0.2)
        raise EngineError(f"The engine didn't answer within {int(wait)} seconds. See {config.LOG_FILE}.")


def ensure_running() -> None:
    if probe() != "aether" or needs_upgrade():
        start()


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop() -> str:
    pid = _read_pid()
    if probe() != "aether":
        config.PID_FILE.unlink(missing_ok=True)
        return "Engine isn't running."
    if pid is None or not _alive(pid):
        return (f"An engine is answering on port {config.PORT}, but it wasn't started by `aether-cache start`, "
                f"so it wasn't stopped. Stop it the way you started it.")
    os.kill(pid, signal.SIGTERM)  # safe: with appendfsync always, every acknowledged write is on disk already
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    else:
        if os.name != "nt":
            os.kill(pid, signal.SIGKILL)
    config.PID_FILE.unlink(missing_ok=True)
    return "Engine stopped."


def running_pid() -> Optional[int]:
    pid = _read_pid()
    return pid if pid is not None and _alive(pid) else None
