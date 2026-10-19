"""
Builds the engine jar that ships inside the Python package: mnemolith/engine/mnemolith-engine.jar.

Run it after changing src/main/java/Main.java:   python scripts/build_engine.py
Needs a JDK (javac + jar). The jar targets Java 11, so it runs on any Java 11+ runtime.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "main" / "java" / "Main.java"
JAR = ROOT / "mnemolith" / "engine" / "mnemolith-engine.jar"


def main() -> int:
    for tool in ("javac", "jar"):
        if not shutil.which(tool):
            print(f"{tool} not found. Install a JDK (11 or newer) and try again.")
            return 1
    with tempfile.TemporaryDirectory() as classes:
        subprocess.run(["javac", "--release", "11", "-encoding", "UTF-8", "-nowarn",
                        "-d", classes, str(SOURCE)], check=True)
        JAR.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["jar", "--create", "--file", str(JAR), "--main-class", "Main", "-C", classes, "."],
                       check=True)
    print(f"Built {JAR.relative_to(ROOT)} ({JAR.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
