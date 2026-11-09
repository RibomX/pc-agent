"""Load and save the configuration (config.json in the user's app data folder)."""

import base64
import copy
import ctypes
import json
import os
import secrets
import sys
from ctypes import wintypes
from pathlib import Path

# Portable copy ("PC Agent Portable.exe", or a portable.txt next to the exe): keeps everything in a
# "data" folder next to the exe and never installs.
_EXE = Path(sys.executable)
PORTABLE = getattr(sys, "frozen", False) and (
    "portable" in _EXE.stem.lower() or (_EXE.parent / "portable.txt").exists())

if os.environ.get("PC_AGENT_CONFIG_DIR"):
    CONFIG_DIR = Path(os.environ["PC_AGENT_CONFIG_DIR"])  # a separate profile, e.g. for testing the first run
elif PORTABLE:
    CONFIG_DIR = Path(sys.executable).parent / "data"
elif getattr(sys, "frozen", False):
    # Packaged .exe – keep settings in %APPDATA%\PC Agent so they survive updates.
    CONFIG_DIR = Path(os.environ["APPDATA"]) / "PC Agent"
else:
    CONFIG_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    # Secret in the URL – without it nobody can reach the app or the MCP server.
    "secret": None,
    "port": 8765,
    # AI providers in the order they are tried; when one runs out of quota the next one takes over.
    # Keys are entered in the app under Settings. Empty model = picked automatically.
    "providers": [
        {"type": "gemini", "key": "", "model": "", "enabled": True},
        {"type": "openrouter", "key": "", "model": "", "enabled": True},
        {"type": "groq", "key": "", "model": "", "enabled": True},
        {"type": "anthropic", "key": "", "model": "", "enabled": True},
        {"type": "openai", "key": "", "model": "", "enabled": True},
        # Set by Settings > "Find local AI" (LM Studio or Ollama on this PC).
        {"type": "local", "key": "", "model": "", "enabled": False, "base_url": ""},
    ],
    # "fast" = Gemini Flash-Lite, ~1 s per step, high daily quota; "smart" = Flash, more accurate but slower.
    "mode": "fast",
    # Remote access through a free Cloudflare tunnel: phone from anywhere + MCP connector for your own AI.
    "remote_access": False,
    # Relay that gives this PC an address that never changes (relay/worker.js). Empty = none.
    # Not set by default: each user runs their own relay (see README), so nobody shares one free quota.
    "relay_url": "",
    # Start the app (minimized to tray) when Windows starts.
    "autostart": False,
    # Access from a phone on the same Wi-Fi (QR code in the app).
    "lan_access": True,
    # Public tunnel domains (e.g. "my-pc.ngrok-free.app") for access from anywhere / MCP connector.
    "allowed_hosts": [],
    # Max width of the screenshot sent to the AI (smaller = less quota, worse accuracy).
    "screenshot_max_width": 1280,
    "screenshot_jpeg_quality": 70,
    # How many times per second the app captures the screen in the background.
    "live_fps": 4,
    # Hotkey that instantly pauses / resumes the AI.
    "pause_hotkey": "ctrl+alt+p",
}


# ---- secrets on disk are encrypted with Windows DPAPI ----
# Only this Windows user can decrypt them, and only together with this app-specific value, so a
# command that simply reads config.json (e.g. an AI tricked by a web page) gets nothing usable.

_ENTROPY = b"PC Agent settings v1"
_PREFIX = "dpapi:"


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source, entropy, out = _blob(data), _blob(_ENTROPY), _Blob()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(out)):  # 0x1 = no UI
        raise OSError("Could not " + ("encrypt" if encrypt else "decrypt") + " a saved secret.")
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        kernel32.LocalFree(out.data)


def protect(value: str) -> str:
    if not value or value.startswith(_PREFIX):
        return value
    return _PREFIX + base64.b64encode(_dpapi(value.encode(), encrypt=True)).decode()


def unprotect(value: str) -> str:
    if not value or not value.startswith(_PREFIX):
        return value  # plain value from an older version; encrypted on the next save
    return _dpapi(base64.b64decode(value[len(_PREFIX):]), encrypt=False).decode()


def _map_secrets(cfg: dict, fn) -> dict:
    out = copy.deepcopy(cfg)
    for field in ("secret", "relay_token", "phone_pin"):
        if out.get(field):
            out[field] = fn(out[field])
    for p in out.get("providers", []):
        if p.get("key"):
            p["key"] = fn(p["key"])
    return out


def save_config(cfg: dict):
    data = json.dumps(_map_secrets(cfg, protect), indent=2, ensure_ascii=False)
    # Write a temporary file and swap it in, so nobody ever reads a half-written config.json.
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def load_config(save: bool = True) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(_map_secrets(json.loads(CONFIG_PATH.read_text(encoding="utf-8")), unprotect))
    # Providers added in newer versions appear at the end of the user's list.
    known = {p["type"] for p in cfg["providers"]}
    cfg["providers"] += [copy.deepcopy(p) for p in DEFAULTS["providers"] if p["type"] not in known]
    if not cfg["secret"]:
        cfg["secret"] = secrets.token_urlsafe(24)
    # Older versions had a single Gemini key.
    if cfg.get("gemini_api_key"):
        gemini = next(p for p in cfg["providers"] if p["type"] == "gemini")
        gemini["key"] = gemini["key"] or cfg["gemini_api_key"]
        gemini["model"] = gemini["model"] or cfg.get("gemini_model", "")
    for old in ("gemini_api_key", "gemini_model", "last_working_model"):
        cfg.pop(old, None)
    if save:
        save_config(cfg)
    return cfg
