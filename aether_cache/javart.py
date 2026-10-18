"""
A private copy of Java, for people who don't have it.

The engine needs Java 11 or newer. If this computer has one, it's used and nothing is downloaded. If not,
`aether-cache setup` downloads Eclipse Temurin (the standard free build of Java) into ~/.aether-cache/java,
checks the download against the checksum its publisher lists, and uses it only for Aether Cache: nothing is
installed system-wide, and `aether-cache uninstall` removes it.
"""
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

from . import config

FEATURE_VERSION = 21  # a long-term-support release
API = os.environ.get("AETHER_JAVA_API", "https://api.adoptium.net/v3")
JAVA_DIR = config.HOME / "java"


class JavaDownloadError(Exception):
    pass


def private_java() -> Optional[Path]:
    """The java program inside our private copy, if it has been downloaded."""
    name = "java.exe" if os.name == "nt" else "java"
    for pattern in (f"*/Contents/Home/bin/{name}", f"*/bin/{name}"):  # macOS keeps it inside a bundle
        for p in sorted(JAVA_DIR.glob(pattern)):
            if p.is_file():
                return p
    return None


def _target() -> tuple:
    """(os, architecture) in the names the download service uses."""
    system = platform.system()
    machine = platform.machine().lower()
    os_name = {"Darwin": "mac", "Windows": "windows", "Linux": "linux"}.get(system)
    if os_name == "linux" and Path("/etc/alpine-release").exists():
        os_name = "alpine-linux"
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    if not os_name or not arch:
        raise JavaDownloadError(f"There's no ready-made Java download for this computer ({system} {machine}).")
    return os_name, arch


def _client():
    import httpx  # installed with the MCP library; respects HTTPS_PROXY like the model download does
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=60.0))


def _find_package(client) -> dict:
    os_name, arch = _target()
    url = f"{API}/assets/latest/{FEATURE_VERSION}/hotspot"
    r = client.get(url, params={"architecture": arch, "image_type": "jre", "os": os_name, "vendor": "eclipse"})
    r.raise_for_status()
    try:
        package = r.json()[0]["binary"]["package"]
        return {"link": package["link"], "sha256": package["checksum"].lower(),
                "size": int(package.get("size") or 0), "name": package["name"]}
    except (ValueError, LookupError, TypeError, AttributeError):
        raise JavaDownloadError(f"The Java download service sent an unexpected answer for {os_name} {arch}.")


def _download(client, pkg: dict, dest: Path, progress: Callable[[int, int], None]) -> None:
    digest = hashlib.sha256()
    done = 0
    with client.stream("GET", pkg["link"]) as r:
        r.raise_for_status()
        total = pkg["size"] or int(r.headers.get("content-length") or 0)
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress(done, total)
    if digest.hexdigest() != pkg["sha256"]:
        raise JavaDownloadError("The Java download didn't match its published checksum, so it wasn't used. "
                                "Run `aether-cache setup` to try again; if it keeps happening, install Java 11+ "
                                f"yourself from https://adoptium.net.\nStill stuck? Tell us: {config.CONTACT_URL}")


def _extract(archive: Path, into: Path) -> None:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():  # refuse paths that would land outside the folder
                if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                    raise JavaDownloadError("The Java download contained an unsafe file path.")
            z.extractall(into)
    else:
        with tarfile.open(archive) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(into, filter="data")  # rejects unsafe paths and links
            else:
                for member in t.getmembers():
                    if member.name.startswith("/") or ".." in Path(member.name).parts:
                        raise JavaDownloadError("The Java download contained an unsafe file path.")
                t.extractall(into)
    if os.name != "nt":  # make sure the programs are runnable (zip files drop permissions)
        for bin_dir in into.glob("*/**/bin"):
            for f in bin_dir.iterdir():
                if f.is_file():
                    f.chmod(f.stat().st_mode | 0o755)


_last_shown = [-1]


def _bar(done: int, total: int) -> None:
    if not sys.stdout.isatty():
        return
    step = int(done * 100 / total) if total else done >> 20  # redraw once per percent (or per MB)
    if step == _last_shown[0]:
        return
    _last_shown[0] = step
    mb = done / 1_048_576
    if total:
        width = 24
        filled = int(width * done / total)
        line = f"         [{'#' * filled}{'.' * (width - filled)}] {mb:5.1f} of {total / 1_048_576:.0f} MB"
    else:
        line = f"         {mb:5.1f} MB"
    print("\r" + line, end="", flush=True)


def download(progress: Callable[[int, int], None] = _bar, attempts: int = 3) -> Path:
    """Downloads and unpacks Java into ~/.aether-cache/java. Returns the path to its java program."""
    config.HOME.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(1, attempts + 1):
        work = Path(tempfile.mkdtemp(prefix="java-download-", dir=config.HOME))
        try:
            with _client() as client:
                pkg = _find_package(client)
                archive = work / pkg["name"]
                _download(client, pkg, archive, progress)
            if progress is _bar and sys.stdout.isatty():
                print()
            unpacked = work / "unpacked"
            unpacked.mkdir()
            _extract(archive, unpacked)
            if JAVA_DIR.exists():
                shutil.rmtree(JAVA_DIR)
            os.replace(unpacked, JAVA_DIR)  # appears all at once, so a half-unpacked Java is never used
            java = private_java()
            if java is None:
                raise JavaDownloadError("The Java download didn't contain a java program.")
            return java
        except JavaDownloadError:
            raise  # a bad checksum or a bad archive won't fix itself by retrying
        except Exception as e:  # network trouble: try again
            last_error = e
            if progress is _bar and sys.stdout.isatty():
                print()
            if attempt < attempts:
                time.sleep(2 * attempt)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    raise JavaDownloadError(
        f"Couldn't download Java from adoptium.net ({_short(last_error)}).\n"
        f"If your network blocks adoptium.net or github.com (common on work or school networks), try another\n"
        f"network and run `aether-cache setup` again, or install Java 11+ yourself from https://adoptium.net.\n"
        f"Still stuck? Tell us: {config.CONTACT_URL}")


def _short(error) -> str:
    text = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    return text[:160]


def remove() -> bool:
    if JAVA_DIR.exists():
        shutil.rmtree(JAVA_DIR, ignore_errors=True)
        return True
    return False
