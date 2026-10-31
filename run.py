"""Εκκίνηση όλων των υπηρεσιών με μία εντολή:  python run.py

* FastAPI (uvicorn) — API + οθόνη σάρωσης  -> http://<server>:8000/scanner/
* Streamlit       — Γραμματεία/Ιστορικό/Ρυθμίσεις -> http://<server>:8501
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402


def lan_addresses() -> list[str]:
    addrs: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # δεν στέλνει πακέτα· βρίσκει τη διεπαφή LAN
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


def main() -> int:
    s = get_settings()
    missing = [r for r in ("admin", "secretary", "scanner") if r not in s.passwords]
    if missing:
        print("ΠΡΟΣΟΧΗ: δεν έχουν οριστεί κωδικοί για τους ρόλους:", ", ".join(missing))
        print("         Αντιγράψτε το .env.example σε .env και συμπληρώστε τους κωδικούς.\n")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONUTF8", "1")

    api_cmd = [sys.executable, "-m", "uvicorn", "app.api.main:app", "--host", s.api_host, "--port", str(s.api_port),
               "--workers", "1", "--log-level", "warning", "--no-access-log"]
    ui_cmd = [sys.executable, "-m", "streamlit", "run", str(ROOT / "app" / "ui" / "streamlit_app.py"),
              "--server.address", s.api_host, "--server.port", str(s.ui_port), "--server.headless", "true",
              "--browser.gatherUsageStats", "false", "--server.fileWatcherType", "none"]

    procs: list[tuple[str, subprocess.Popen]] = []
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    procs.append(("API", subprocess.Popen(api_cmd, cwd=ROOT, env=env, **kwargs)))
    time.sleep(1.5)
    procs.append(("UI", subprocess.Popen(ui_cmd, cwd=ROOT, env=env, **kwargs)))

    hosts = ["localhost"] + lan_addresses()
    print("=" * 70)
    print(" Εφαρμογή αποθήκης σε λειτουργία. Δεδομένα:", s.data_dir)
    print("=" * 70)
    for h in hosts:
        print(f"  Οθόνη σάρωσης (αποθήκη):    http://{h}:{s.api_port}/scanner/")
        print(f"  Γραμματεία / Ρυθμίσεις:     http://{h}:{s.ui_port}")
    print("  Τερματισμός: Ctrl+C")
    print("=" * 70)

    stopping = False

    def stop(*_a) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    restarts = {"API": 0, "UI": 0}
    try:
        while not stopping:
            time.sleep(1)
            for i, (name, p) in enumerate(procs):
                if p.poll() is not None and not stopping:
                    restarts[name] += 1
                    print(f"[run.py] Η υπηρεσία {name} σταμάτησε (κωδικός {p.returncode}). Επανεκκίνηση #{restarts[name]}…")
                    time.sleep(min(10, 2 * restarts[name]))
                    cmd = api_cmd if name == "API" else ui_cmd
                    procs[i] = (name, subprocess.Popen(cmd, cwd=ROOT, env=env, **kwargs))
    finally:
        print("\nΤερματισμός υπηρεσιών…")
        for _name, p in procs:
            if p.poll() is None:
                p.terminate()
        for _name, p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
