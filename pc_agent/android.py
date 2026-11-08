"""Control an Android phone through ADB wireless debugging (free, no root, Android 11+).

The phone stays connected to this PC over Wi-Fi; the AI sees its screen and taps, swipes and
types on it. Google's official platform-tools (adb) are downloaded once, when the user pairs a phone.
"""

import functools
import io
import logging
import re
import secrets
import shlex
import shutil
import struct
import subprocess
import threading
import time
import urllib.request
import zipfile

from PIL import Image

from . import actions, verify
from .config import CONFIG_DIR, save_config

log = logging.getLogger("pc-agent")

QR_TIMEOUT_S = 180
KEEP_CONNECTED_S = 20

PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
TOOLS_DIR = CONFIG_DIR / "platform-tools"
MAX_SIDE = 1280  # longest side of the screenshot sent to the AI

KEYS = {"back": 4, "home": 3, "recents": 187, "enter": 66, "delete": 67, "backspace": 67, "tab": 61,
        "power": 26, "volume_up": 24, "volume_down": 25, "search": 84, "menu": 82, "escape": 111}


class PhoneError(Exception):
    pass


MAX_PHONE_PIXELS = 4096 * 4096


def _png_screencap(data: bytes) -> Image.Image | None:
    """`screencap -p` output. PNG is ~30x smaller than raw pixels (about 1 s instead of 11 s over Wi-Fi).
    Only the PNG decoder is allowed and the size is checked before decoding, so a broken or hostile
    file cannot reach Pillow's other decoders."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        return None
    width, height = struct.unpack(">II", data[16:24])  # IHDR, before any pixel data is decoded
    if not (0 < width <= 8192 and 0 < height <= 8192) or width * height > MAX_PHONE_PIXELS:
        return None
    return Image.open(io.BytesIO(data), formats=["PNG"]).convert("RGB")


class Phone:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.serial: str | None = None
        self.status = "off"  # off | downloading | ready | connected
        self.model = ""
        self.qr_name = ""
        self.qr_password = ""
        self.qr_state = ""  # "" | waiting | connected | timeout | failed
        self._lock = threading.Lock()
        self._frame: tuple[float, bytes, tuple[int, int], float] | None = None  # (time, jpeg, size, scale)
        self._awake_checked = 0.0
        self._wake_lock = threading.Lock()
        self.pin_failed = False  # the saved PIN did not unlock the phone – never retry it (lockout)

    # ---- adb ----
    def adb_path(self) -> str:
        local = TOOLS_DIR / "adb.exe"
        if local.exists():
            verify.require(local, "Google LLC")
            return str(local)
        found = shutil.which("adb")
        if found:
            return found
        self.status = "downloading"
        zip_path = CONFIG_DIR / "platform-tools.zip"
        urllib.request.urlretrieve(PLATFORM_TOOLS_URL, zip_path)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(CONFIG_DIR)  # the zip contains a platform-tools/ folder
        zip_path.unlink()
        verify.require(local, "Google LLC")
        self.status = "ready"
        return str(local)

    def adb(self, *args: str, serial: bool = True, timeout: float = 20, binary: bool = False,
            stdin: bytes | None = None):
        cmd = [self.adb_path()]
        if serial and self.serial:
            cmd += ["-s", self.serial]
        proc = subprocess.run(cmd + list(args), capture_output=True, timeout=timeout, input=stdin,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        if binary:
            return proc.stdout
        return (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()

    # ---- connection ----
    def pair(self, address: str, code: str) -> str:
        out = self.adb("pair", address.strip(), code.strip(), serial=False, timeout=30)
        if "Successfully paired" not in out:
            raise PhoneError(f"Pairing failed: {out[:200]}")
        return "Paired. Now connect with the address shown under Wireless debugging."

    def connect(self, address: str) -> str:
        address = address.strip()
        out = self.adb("connect", address, serial=False, timeout=20)
        if "connected" not in out or "cannot" in out or "failed" in out:
            raise PhoneError(f"Could not connect: {out[:200]}")
        self.serial = address
        self.model = self.adb("shell", "getprop", "ro.product.model")
        self.status = "connected"
        self.cfg["phone_address"] = address
        self.cfg["phone_paired"] = True
        self.cfg.pop("phone_disconnected", None)
        # The service name stays the same when the phone's address or port changes.
        service = next((name for name, kind, found in self._services() if kind == "connect" and found == address), None)
        if service:
            self.cfg["phone_service"] = service
        return f"Connected to {self.model}."

    def reconnect_saved(self):
        """Try the last phone again; wireless debugging ports change after a phone restart,
        so also look for the same phone on the network."""
        address = self.cfg.get("phone_address")
        if not address:
            return
        try:
            self.connect(address)
        except Exception as e:
            log.debug(f"phone not reachable: {e}")
            found = self._find_service("connect", name=self.cfg.get("phone_service")) \
                or self._find_service("connect", ip=address.split(":")[0])
            if found:
                try:
                    self.connect(found)
                    save_config(self.cfg)
                except Exception as e2:
                    log.debug(f"phone not reachable: {e2}")

    def adopt_connected(self):
        """A phone already connected over wireless debugging (e.g. before the app restarted) is used again."""
        if self.cfg.get("phone_disconnected"):
            return  # the user pressed Disconnect
        out = self.adb("devices", serial=False)
        address = next((m.group(1) for m in re.finditer(r"^([\d.]+:\d+)\s+device$", out, re.M)), None)
        if address:
            self.connect(address)
            save_config(self.cfg)

    def keep_connected(self):
        """Background loop: notices a lost phone and connects it again without the user doing anything."""
        while True:
            # Privacy: the last phone screenshot is not kept longer than needed.
            if self._frame and time.time() - self._frame[0] > KEEP_CONNECTED_S:
                self._frame = None
            try:
                if self.connected and self.adb("get-state") != "device":
                    self.status = "off"
                if not self.connected and self.cfg.get("phone_address") and self.qr_state != "waiting":
                    self.reconnect_saved()
                elif not self.connected and (TOOLS_DIR / "adb.exe").exists() and self.qr_state != "waiting":
                    self.adopt_connected()
            except Exception as e:
                log.info(f"phone check: {e}")
            time.sleep(KEEP_CONNECTED_S)

    # ---- pairing by QR code (the phone's "Pair device with QR code" screen) ----
    def _services(self) -> list[tuple[str, str, str]]:
        """Wireless-debugging services on the network: (name, pairing|connect, ip:port)."""
        out = self.adb("mdns", "services", serial=False)
        return [(m[1], m[2], m[3]) for m in re.finditer(
            r"^(\S+)\s+_adb-tls-(pairing|connect)\._tcp\.?\s+([\d.]+:\d+)", out, re.M)]

    def _find_service(self, kind: str, name: str | None = None, ip: str | None = None) -> str | None:
        for found_name, found_kind, address in self._services():
            if found_kind == kind and (name is None or found_name == name) and (ip is None or address.startswith(ip + ":")):
                return address
        return None

    def start_qr_pairing(self) -> str:
        """Start waiting for the phone; returns the text to show as a QR code."""
        self.qr_name = f"pcagent-{secrets.token_hex(3)}"
        self.qr_password = secrets.token_hex(6)
        self.qr_state = "waiting"
        threading.Thread(target=self._qr_loop, args=(self.qr_name, self.qr_password), daemon=True).start()
        return self.qr_text()

    def qr_text(self) -> str:
        # The format Android's "Pair device with QR code" scanner expects.
        return f"WIFI:T:ADB;S:{self.qr_name};P:{self.qr_password};;"

    def _qr_loop(self, name: str, password: str):
        try:
            deadline = time.time() + QR_TIMEOUT_S
            while time.time() < deadline and self.qr_name == name:
                address = self._find_service("pairing", name=name)
                if address:
                    self.pair(address, password)
                    ip = address.split(":")[0]
                    while time.time() < deadline:
                        connect_address = self._find_service("connect", ip=ip)
                        if connect_address:
                            self.connect(connect_address)
                            save_config(self.cfg)
                            self.qr_state = "connected"
                            return
                        time.sleep(1)
                    break
                time.sleep(1)
            if self.qr_name == name:
                self.qr_state = "timeout"
        except Exception as e:
            log.warning(f"QR pairing: {e}")
            self.qr_state = "failed"

    def disconnect(self):
        if self.serial:
            self.adb("disconnect", self.serial, serial=False)
        self.serial, self.status, self.model = None, "off", ""
        self.cfg.pop("phone_address", None)  # the user chose this, do not reconnect by itself
        self.cfg["phone_disconnected"] = True

    @property
    def connected(self) -> bool:
        return self.status == "connected" and self.serial is not None

    # ---- screen ----
    def capture(self, max_age: float = 0.3) -> tuple[bytes, tuple[int, int], float]:
        """JPEG of the phone screen, its size and the scale (phone pixels per screenshot pixel)."""
        if not self.connected:
            raise PhoneError("No phone is connected.")
        with self._lock:
            if self._frame and time.time() - self._frame[0] < max_age:
                return self._frame[1:]
            img = _png_screencap(self.adb("exec-out", "screencap", "-p", binary=True, timeout=30))
            if img is None:
                self.status = "off"
                raise PhoneError("The phone does not answer – is wireless debugging still on?")
            scale = max(max(img.size) / MAX_SIDE, 1.0)
            if scale > 1:
                img = img.resize((round(img.width / scale), round(img.height / scale)))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            self._frame = (time.time(), buf.getvalue(), img.size, scale)
            return self._frame[1:]

    def jpeg(self) -> bytes:
        return self.capture()[0]

    @property
    def shot_size(self) -> tuple[int, int]:
        return self.capture(max_age=5)[1]

    def _px(self, x: float, y: float) -> tuple[str, str]:
        scale = self.capture(max_age=5)[2]
        return str(round(x * scale)), str(round(y * scale))

    # ---- screen off / lock screen ----
    def _lock_state(self) -> tuple[bool, bool, bool]:
        """(screen on, lock screen showing, lock needs PIN/password/pattern)"""
        out = self.adb("shell", "dumpsys power | grep -m1 mWakefulness=; "
                                "dumpsys window policy | grep -E '^ +(showing|secure)='")
        awake = "mWakefulness=Awake" in out
        showing = bool(re.search(r"^\s*showing=true", out, re.M))
        secure = bool(re.search(r"^\s*secure=true", out, re.M))
        return awake, showing, secure

    def ensure_awake(self, force: bool = False) -> str | None:
        """Before the AI looks or acts: turn the screen on and get past the lock screen.
        The saved phone password is typed by the app itself; the AI never sees it, and while the lock
        screen shows, no other action runs, so the AI cannot type anything else there.
        Returns a problem or None."""
        if not self.connected:
            return None
        with self._wake_lock:
            if not force and time.time() - self._awake_checked < 3:
                return None
            awake, showing, secure = self._lock_state()
            if awake and not showing:
                self._awake_checked = time.time()
                return None
            self._frame = None
            if not awake:
                self.adb("shell", "input", "keyevent", "224")  # WAKEUP (never turns the screen off)
                time.sleep(0.6)
            if showing:
                self.adb("shell", "wm", "dismiss-keyguard")
                time.sleep(0.8)
                pin = self.cfg.get("phone_pin") or ""
                if secure:
                    if not pin:
                        return ("The phone is locked with a PIN and no phone password is saved. Tell the user to "
                                "unlock it, or to save it in PC Agent Settings > Advanced > Phone password.")
                    if self.pin_failed:
                        return ("The phone is locked and the saved phone password did not work. The user must "
                                "fix it in PC Agent Settings > Advanced > Phone password.")
                    # Sent through stdin, so the PIN never shows up in a command line.
                    self.adb("shell", "read -r p; input text \"$p\"; input keyevent 66",
                             stdin=(pin + "\n").encode())
                    time.sleep(1.2)
                    if self._lock_state()[1]:
                        self.pin_failed = True  # never try again: wrong tries lock the phone for a while
                        log.warning("phone: the saved phone password did not unlock the phone")
                        return ("The phone is locked and the saved phone password did not work. The user must "
                                "fix it in PC Agent Settings > Advanced > Phone password.")
                elif self._lock_state()[1]:
                    w, h = self.adb("shell", "wm", "size").split()[-1].split("x")
                    self.adb("shell", "input", "swipe", str(int(w) // 2), str(int(h) * 4 // 5),
                             str(int(w) // 2), str(int(h) // 5), "250")
                    time.sleep(0.6)
            self._frame = None
            self._awake_checked = time.time()
            return None

    # ---- input ----
    def tap(self, x, y):
        self.adb("shell", "input", "tap", *self._px(x, y))

    def long_press(self, x, y, ms=700):
        px = self._px(x, y)
        self.adb("shell", "input", "swipe", *px, *px, str(ms))

    def swipe(self, x1, y1, x2, y2, ms=300):
        self.adb("shell", "input", "swipe", *self._px(x1, y1), *self._px(x2, y2), str(ms))

    def text(self, text: str):
        # adb joins its arguments into one command for the phone's shell, so every value from the AI
        # is quoted as a whole. `input text` wants %s for spaces and handles ASCII only.
        printable = "".join(ch for ch in text if 32 <= ord(ch) < 127)
        self.adb("shell", "input text " + shlex.quote(printable.replace(" ", "%s")))

    def key(self, name: str):
        code = KEYS.get(name.lower().replace(" ", "_"))
        if code is None:
            raise PhoneError(f"Unknown key {name}. Use one of: {', '.join(KEYS)}")
        self.adb("shell", "input", "keyevent", str(code))

    def packages(self) -> list[str]:
        out = self.adb("shell", "pm", "list", "packages")
        return sorted(line.removeprefix("package:") for line in out.splitlines() if line.startswith("package:"))

    def launch(self, name: str) -> str:
        q = re.sub(r"[^a-z0-9]", "", name.lower())
        pkgs = self.packages()
        matches = [p for p in pkgs if q and q in re.sub(r"[^a-z0-9]", "", p.split(".")[-1])] or \
                  [p for p in pkgs if q and q in re.sub(r"[^a-z0-9]", "", p)]
        if not matches:
            return f'No app matches "{name}". Use phone_list_apps to see package names.'
        pkg = min(matches, key=len)
        self.adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
        return f"Launched {pkg}."

    def open_url(self, url: str):
        if not re.match(r"https?://", url, re.I):
            raise PhoneError("Only http and https addresses can be opened.")
        self.adb("shell", "am start -a android.intent.action.VIEW -d " + shlex.quote(url))


phone: Phone = None  # set in main.py


# ---------------------------------------------------------------- AI tools
# Coordinates are pixels of the phone screenshot the AI sees (the agent converts Gemini's 0-1000 grid).


def _settle(seconds: float = 0.6):
    time.sleep(seconds)


def _phone_tool(fn):
    """The pause button (Ctrl+Alt+P) stops phone actions too, and each one is logged."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        actions._guard(f"{fn.__name__} {args or ''}{kwargs or ''}")
        if fn.__name__ not in ("phone_wait", "phone_list_apps"):
            # Typing always re-checks, so nothing typed by the AI can ever land on the lock screen.
            problem = phone.ensure_awake(force=fn.__name__ in ("phone_type", "phone_unlock"))
            if problem:
                return problem
        return fn(*args, **kwargs)

    return wrapper


@_phone_tool
def phone_tap(x: int, y: int) -> str:
    """Tap a point on the phone screenshot."""
    phone.tap(x, y)
    _settle()
    return f"Tapped ({x}, {y})."


@_phone_tool
def phone_long_press(x: int, y: int) -> str:
    """Press and hold a point on the phone screenshot (context menus, moving icons)."""
    phone.long_press(x, y)
    _settle()
    return "Long-pressed."


@_phone_tool
def phone_swipe(from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 300) -> str:
    """Swipe on the phone. Scroll down = swipe from lower to upper part of the screen."""
    phone.swipe(from_x, from_y, to_x, to_y, duration_ms)
    _settle()
    return "Swiped."


@_phone_tool
def phone_type(text: str, press_enter: bool = False) -> str:
    """Type text into the focused field on the phone (tap the field first). English letters, digits
    and basic symbols only."""
    phone.text(text)
    if press_enter:
        phone.key("enter")
    _settle(0.3)
    return "Typed."


@_phone_tool
def phone_key(key: str) -> str:
    """Press a phone button: back, home, recents, enter, delete, tab, power, volume_up, volume_down, search."""
    phone.key(key)
    _settle()
    return f"Pressed {key}."


@_phone_tool
def phone_unlock() -> str:
    """Turn the phone's screen on and unlock it with the phone password the user saved in PC Agent.
    This is the only way to unlock the phone: never type a PIN or password on the lock screen yourself.
    (Every phone action already does this by itself; use it when you see the lock screen.)"""
    return "The phone is on and unlocked."


@_phone_tool
def phone_launch_app(name: str) -> str:
    """Open an app on the phone by name, e.g. "youtube", "whatsapp", "chrome", "settings", "camera"."""
    result = phone.launch(name)
    _settle(1.5)
    return result


@_phone_tool
def phone_list_apps(filter: str = "") -> str:
    """List installed app package names on the phone (optionally only those containing `filter`)."""
    pkgs = [p for p in phone.packages() if filter.lower() in p.lower()]
    return "\n".join(pkgs[:300]) or "Nothing found."


@_phone_tool
def phone_open_url(url: str) -> str:
    """Open a web address or deep link on the phone, e.g. "https://www.google.com/search?q=weather"."""
    phone.open_url(url)
    _settle(1.5)
    return "Opened."


@_phone_tool
def phone_wait(seconds: float) -> str:
    """Wait (e.g. while an app loads). Max 60 s."""
    time.sleep(min(seconds, 60))
    return f"Waited {seconds} s."


PHONE_TOOLS = [phone_tap, phone_long_press, phone_swipe, phone_type, phone_key, phone_unlock, phone_launch_app,
               phone_list_apps, phone_open_url, phone_wait]


class PhoneScreen:
    """Same interface the agent uses for the PC screen, backed by the phone."""

    is_black = False

    def jpeg(self) -> bytes:
        phone.ensure_awake()
        return phone.jpeg()

    @property
    def shot_size(self):
        return phone.shot_size
