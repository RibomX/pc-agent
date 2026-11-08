"""PC Agent desktop app: app window + tray icon + background server (UI, agent, MCP).

Flags:
  --minimized   start hidden in the tray (used by autostart)
  --no-window   server only, no window or tray (for development/tests)
"""

import ctypes
import logging
import os
import socket
import sys
import threading
import urllib.request

from .config import CONFIG_DIR, CONFIG_PATH, load_config

# A windowed .exe has no console – send all output to a log file instead.
LOG_PATH = CONFIG_DIR / "pc-agent.log"
NO_CONSOLE = sys.stdout is None or sys.stderr is None
if NO_CONSOLE:
    sys.stdout = sys.stderr = open(LOG_PATH, "a", encoding="utf-8", buffering=1)

# Must run before pyautogui is imported so mouse coordinates match screenshots at 125 %/150 % scaling.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import keyboard  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

from . import actions, android, apps, autostart  # noqa: E402
from .agent import Agent  # noqa: E402
from .screen import LiveScreen  # noqa: E402
from .tools import build_server  # noqa: E402
from .tunnel import Tunnel  # noqa: E402
from .web import register_routes  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
    + ([] if NO_CONSOLE else [logging.FileHandler(LOG_PATH, encoding="utf-8")]),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
log = logging.getLogger("pc-agent")


def lan_ip() -> str | None:
    """This PC's address on the home network (a phone on the same Wi-Fi connects through it)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.0.1", 80))  # nothing is sent, this only picks the outgoing address
            return s.getsockname()[0]
    except OSError:
        return None


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def running_window() -> int:
    """The window of an already running PC Agent (also when it is hidden in the tray), or 0."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    me = os.getpid()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def each(hwnd, _):
        title = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(hwnd, title, 64)
        if title.value == "PC Agent":
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # Only a window that really belongs to PC Agent (not e.g. a folder named "PC Agent").
            handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle and pid.value != me:
                path = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    if os.path.basename(path.value).lower() in ("pc agent.exe", "python.exe", "pythonw.exe"):
                        found.append(hwnd)
            if handle:
                kernel32.CloseHandle(handle)
        return True

    user32.EnumWindows(each, 0)
    return found[0] if found else 0


def already_running(cfg: dict) -> bool:
    """If PC Agent already runs, bring its window to the front and return True.
    The port decides: a slow answer must never start a second copy that cannot use the port."""
    if not port_in_use(cfg["port"]):
        return False
    user32 = ctypes.windll.user32
    # This copy was just started by the user, so Windows lets it put a window in front. It does that
    # itself, so it works even if the running copy is busy or does not answer.
    hwnd = running_window()
    if hwnd:
        user32.ShowWindow(hwnd, 5)  # SW_SHOW (it may be hidden in the tray)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    # Also tell the running copy, so its own idea of "window shown" stays right.
    user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    url = f"http://127.0.0.1:{cfg['port']}/app/{cfg['secret']}/api/show"
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST"), timeout=10)
    except Exception as e:
        log.warning(f"the running copy did not answer: {e}")
    return True


def main():
    minimized = "--minimized" in sys.argv
    windowed = "--no-window" not in sys.argv and not os.environ.get("PC_AGENT_NO_WINDOW")
    # Read only: a second launch must never rewrite the settings the running copy uses.
    cfg = load_config(save=False)
    if windowed and already_running(cfg):
        return 0
    cfg = load_config()

    port = cfg["port"]
    app_path = f"/app/{cfg['secret']}"
    mcp_path = f"/mcp/{cfg['secret']}"

    screen = LiveScreen(cfg["live_fps"], cfg["screenshot_max_width"], cfg["screenshot_jpeg_quality"])
    screen.start()
    actions.screen = screen
    actions.secrets_provider = lambda: [cfg["secret"], cfg.get("relay_token", ""), cfg.get("phone_pin", "")] + [
        p.get("key", "") for p in cfg["providers"]
    ]
    agent = Agent(cfg)
    android.phone = android.Phone(cfg)
    # Build the list of installed apps/games in advance so the first launch_app is instant,
    # and reconnect the phone used last time.
    threading.Thread(target=apps.all_apps, daemon=True).start()
    threading.Thread(target=android.phone.keep_connected, daemon=True).start()
    if cfg.get("autostart"):
        autostart.set_enabled(True)  # keep the path current if the app was moved or installed
    ui = DesktopUI(agent) if windowed else None

    def toggle_pause():
        actions.State.paused = not actions.State.paused
        agent.emit("status", "AI paused." if actions.State.paused else "AI resumed.")
        if ui:
            ui.refresh_tray()

    keyboard.add_hotkey(cfg["pause_hotkey"], toggle_pause)

    # The home network gets plain http, so it is only used when remote access (https) is off.
    ip = lan_ip() if cfg["lan_access"] and not cfg.get("remote_access") else None
    urls = {
        "local": f"http://127.0.0.1:{port}{app_path}",
        "mobile": f"http://{ip}:{port}{app_path}" if ip else None,
    }
    hosts = ["127.0.0.1:*", "localhost:*"] + ([f"{ip}:*"] if ip else [])
    hosts += [h for host in cfg["allowed_hosts"] for h in (host, f"{host}:*")]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "https://claude.ai"]
        + [f"https://{host}" for host in cfg["allowed_hosts"]],
    )

    def allow_public_host(host: str):
        # The MCP endpoint checks the Host/Origin headers – let the tunnel's address through.
        security.allowed_hosts.append(host)
        security.allowed_origins.append(f"https://{host}")

    tunnel = Tunnel(port, allow_public_host, cfg)
    if cfg.get("remote_access"):
        tunnel.start()

    server = build_server(agent)
    register_routes(
        server, cfg, agent, urls, tunnel,
        on_show=ui.show if ui else None,
        # Quit a moment later so the request that triggered it still gets its answer.
        on_quit=(lambda: threading.Timer(1.0, ui.quit).start()) if ui else None,
    )

    log.info("PC Agent is running")
    log.info(f"Settings: {CONFIG_PATH}")
    log.info(f"Pause the AI: {cfg['pause_hotkey'].upper()}  |  Emergency stop: mouse to the top-left corner")

    def serve():
        server.run("streamable-http", host="0.0.0.0" if ip else "127.0.0.1", port=port,
                   streamable_http_path=mcp_path, transport_security=security)

    if not ui:
        print(f"App: {urls['local']}")
        serve()
        return 0

    threading.Thread(target=serve, daemon=True).start()
    ui.run(urls["local"], minimized)
    tunnel.stop()  # cloudflared is a separate program and would keep running otherwise
    os._exit(0)  # stop the server thread and hotkeys together with the window


class DesktopUI:
    """Native window (WebView2) showing the app + tray icon. Closing the window hides it to the tray."""

    def __init__(self, agent: Agent):
        self.agent = agent
        self.window = None
        self.tray = None
        self.quitting = False
        self.told_about_tray = False

    def run(self, url: str, minimized: bool):
        import webview

        # 80 % of the screen in logical pixels (the process is DPI aware, so metrics are physical).
        user32 = ctypes.windll.user32
        scale = user32.GetDpiForSystem() / 96
        width = int(user32.GetSystemMetrics(0) / scale * 0.8)
        height = int(user32.GetSystemMetrics(1) / scale * 0.8)
        self.window = webview.create_window(
            "PC Agent", url, width=width, height=height, min_size=(420, 500),
            hidden=minimized, background_color="#0f1115",
        )
        self.window.events.closing += self._on_closing
        self._start_tray()
        webview.start(private_mode=False, storage_path=str(CONFIG_DIR / "webview"))

    def _on_closing(self):
        if self.quitting:
            return True
        self.window.hide()
        if not self.told_about_tray and self.tray:
            self.told_about_tray = True
            self.tray.notify("PC Agent keeps running in the tray. Right-click the icon to quit.", "PC Agent")
        return False

    def show(self):
        if self.window:
            self.window.show()
            self.window.restore()
            # Bring it in front of other windows (allowed thanks to AllowSetForegroundWindow above).
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "PC Agent")
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)

    def quit(self):
        self.quitting = True
        if self.tray:
            self.tray.stop()
        if self.window:
            self.window.destroy()

    def _toggle_pause(self):
        actions.State.paused = not actions.State.paused
        self.agent.emit("status", "AI paused." if actions.State.paused else "AI resumed.")
        self.refresh_tray()

    def refresh_tray(self):
        if self.tray:
            from .icon import make_icon

            self.tray.icon = make_icon(64, paused=actions.State.paused)
            self.tray.update_menu()

    def _start_tray(self):
        import pystray

        from .icon import make_icon

        menu = pystray.Menu(
            pystray.MenuItem("Open PC Agent", lambda: self.show(), default=True),
            pystray.MenuItem(
                lambda _: "Resume AI" if actions.State.paused else "Pause AI", lambda: self._toggle_pause()
            ),
            pystray.MenuItem("Stop current task", lambda: self.agent.stop()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.quit()),
        )
        self.tray = pystray.Icon("PC Agent", make_icon(64), "PC Agent", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
