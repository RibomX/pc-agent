"""Remote access through a free Cloudflare quick tunnel (no account needed).

Gives the app a public https address, so the phone works from anywhere (not only on the same
Wi-Fi) and the user's own AI (Claude, Gemini...) can reach the MCP server as a connector.
The tunnel's address changes when it restarts, so the app tells a small relay (relay/worker.js)
the current one; connectors and phone shortcuts use the relay's address, which never changes.
"""

import json
import logging
import re
import secrets
import subprocess
import threading
import time
import urllib.request

from . import verify
from .config import CONFIG_DIR, save_config

log = logging.getLogger("pc-agent")

# Official release from Cloudflare's GitHub, downloaded once when the user turns remote access on.
DOWNLOAD_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
EXE = CONFIG_DIR / "cloudflared.exe"
RESTART_DELAY_S = 5
REGISTER_RETRY_MAX_S = 300


class Tunnel:
    def __init__(self, port: int, on_host, cfg: dict):
        self.port = port
        self.on_host = on_host  # called with the public host name once it is known
        self.cfg = cfg
        self.status = "off"  # off | downloading | starting | on | error
        self.error = ""
        self.host: str | None = None
        self.stable_base: str | None = None  # relay address for this PC, once registered
        self._proc: subprocess.Popen | None = None

    def start(self):
        if self.status in ("downloading", "starting", "on"):
            return
        self.error = ""
        self.status = "starting"
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.status, self.host, self.stable_base = "off", None, None  # first, so the thread stops restarting
        if self._proc:
            self._proc.terminate()
            self._proc = None

    def _run(self):
        try:
            if not EXE.exists():
                self.status = "downloading"
                tmp = EXE.with_suffix(".part")
                urllib.request.urlretrieve(DOWNLOAD_URL, tmp)
                tmp.replace(EXE)
            verify.require(EXE, "Cloudflare, Inc.")
            # Quick tunnels can drop; come back by themselves as long as remote access is on.
            while self.status != "off":
                self.status = "starting"
                self._run_once()
                self.host = None
                if self.status != "off":
                    time.sleep(RESTART_DELAY_S)
        except Exception as e:
            log.warning(f"tunnel: {e}")
            self.status, self.error = "error", f"Could not start remote access: {e}"

    def _run_once(self):
        self._proc = subprocess.Popen(
            [str(EXE), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in self._proc.stdout:
            match = re.search(r"https://([a-z0-9-]+\.trycloudflare\.com)", line)
            if match and not self.host:
                self.host = match.group(1)
                self.on_host(self.host)
                # Separate thread: retries must not stop this loop from reading cloudflared's output.
                threading.Thread(target=self._register, args=(self.host,), daemon=True).start()
                self.status = "on"
                log.info("Remote access is on")

    def _register(self, host: str):
        """Tell the relay where this PC is now. Without a relay the tunnel address is used directly."""
        relay = (self.cfg.get("relay_url") or "").rstrip("/")
        if not relay:
            return
        if not self.cfg.get("relay_id"):
            self.cfg["relay_id"] = secrets.token_urlsafe(16)
            self.cfg["relay_token"] = secrets.token_urlsafe(32)
            save_config(self.cfg)
        body = json.dumps({"id": self.cfg["relay_id"], "token": self.cfg["relay_token"], "host": host}).encode()
        request = urllib.request.Request(f"{relay}/register", data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": "PC-Agent",
        })
        # Keep trying in the background (e.g. the relay's hourly limit), as long as this tunnel lives.
        delay = 2
        while self.host == host and self.status != "off":
            try:
                urllib.request.urlopen(request, timeout=15).close()
                self.stable_base = f"{relay}/{self.cfg['relay_id']}"
                return
            except Exception as e:
                log.warning(f"relay: {e}")
                time.sleep(delay)
                delay = min(delay * 2, REGISTER_RETRY_MAX_S)
