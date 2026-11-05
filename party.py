"""Start a party: the karaoke server plus a Cloudflare tunnel, and print the QR code.

    .venv/bin/python party.py            # internet link via Cloudflare quick tunnel
    .venv/bin/python party.py --local    # Wi-Fi only, no tunnel

Needs `cloudflared` (brew install cloudflared). The tunnel URL is new every time,
so the QR code is too; admins also see it on the /admin page.
Ctrl+C stops everything.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import segno

ROOT = Path(__file__).resolve().parent
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # no packets sent; just picks the LAN interface
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def start_tunnel(port: int) -> tuple[subprocess.Popen, str]:
    exe = shutil.which("cloudflared")
    if not exe:
        sys.exit("cloudflared isn't installed. Run: brew install cloudflared")
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    found: list[str] = []
    ready = threading.Event()

    def watch() -> None:
        assert proc.stderr
        for line in proc.stderr:  # keep draining so cloudflared never blocks
            if not found and (m := TUNNEL_URL.search(line)):
                found.append(m.group(0))
                ready.set()
            if "ERR" in line:
                print("[tunnel]", line.rstrip(), file=sys.stderr)
        ready.set()  # process exited

    threading.Thread(target=watch, daemon=True).start()
    if not ready.wait(45) or not found:
        proc.terminate()
        sys.exit("Couldn't start the Cloudflare tunnel. Check your internet connection and try again.")
    return proc, found[0]


def wait_for_server(port: int, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def join_code() -> str:
    """Read (or create) the current join code the same way the app does."""
    sys.path.insert(0, str(ROOT))
    from app.config import Settings
    from app.store import Store

    return Store(Settings.from_env().db_path).join_code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--local", action="store_true", help="Wi-Fi only; don't start a tunnel")
    args = parser.parse_args()

    # Before the server starts: opening the database while it runs could disturb
    # the song that's playing.
    code = join_code()

    tunnel = None
    if args.local:
        public = f"http://{lan_ip()}:{args.port}"
    else:
        print("Starting Cloudflare tunnel…")
        tunnel, public = start_tunnel(args.port)

    env = {**os.environ, "KQ_PUBLIC_URL": public}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
         "--host", "0.0.0.0", "--port", str(args.port)],
        cwd=ROOT,
        env=env,
    )

    def stop(*_):
        for p in (server, tunnel):
            if p and p.poll() is None:
                p.terminate()
        for p in (server, tunnel):
            if p:
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if not wait_for_server(args.port):
        print("The karaoke server didn't start. See the errors above.")
        stop()

    link = f"{public}/join/{code}"
    print("\n🎤  Scan to join the party:\n")
    segno.make(link, error="m").terminal(compact=True)
    print(f"\n    {link}")
    print(f"    Admin page: {public}/admin  (shows the current QR code, even after 'New link')")
    if not args.local:
        print(f"    On this Wi-Fi you can also use http://{lan_ip()}:{args.port}")
    print("\nCtrl+C to stop.\n")

    while True:
        if server.poll() is not None:
            print("The karaoke server stopped.")
            stop()
        if tunnel and tunnel.poll() is not None:
            print("The Cloudflare tunnel stopped; guests off the Wi-Fi can't reach the party. Restart party.py.")
            stop()
        time.sleep(1)


if __name__ == "__main__":
    main()
