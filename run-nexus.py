"""Portable Python 3.12 bootstrap; no third-party dependencies required."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent
APP = ROOT / "nexus"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=30000)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("Please install Python 3.12 and run this launcher with it.")
    if not 30000 <= args.port <= 65535:
        parser.error("Port must be between 30000 and 65535.")
    os.chdir(ROOT)
    python = APP / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        print("Creating local Python environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(APP / ".venv")
    requirements = (APP / "requirements.txt").read_bytes()
    marker = APP / ".venv" / "nexus-requirements.txt"
    if not marker.exists() or marker.read_bytes() != requirements:
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(APP / "requirements.txt")], check=True)
        marker.write_bytes(requirements)
    config = APP / ".env"
    if not config.exists():
        config.write_text("NEXUS_LLM_ENABLED=false\nNEXUS_LLM_PROVIDER=deepseek\nNEXUS_LLM_API_KEY=\nNEXUS_LLM_BASE_URL=https://api.deepseek.com/v1\nNEXUS_LLM_MODEL=deepseek-chat\nNEXUS_AGENT_LOOP=true\nNEXUS_DATABASE_URL=sqlite+aiosqlite:///./nexus/nexus-demo.db\nNEXUS_PUBLIC_DEMO=false\n", encoding="utf-8")
    if args.prepare:
        return
    print(f"NEXUS: http://127.0.0.1:{args.port}/ (Ctrl+C to stop)", flush=True)
    os.execv(str(python), [str(python), "-m", "uvicorn", "nexus.backend.api.app:app", "--host", "127.0.0.1", "--port", str(args.port)])


if __name__ == "__main__":
    main()
