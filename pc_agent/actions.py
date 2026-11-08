"""PC control actions. Used by the built-in agent (Gemini) and by the MCP server.

Every action returns a text result for the AI. Coordinates x, y are pixels of the screenshot
(the built-in agent converts Gemini's 0-1000 coordinates before calling). Docstrings and type hints matter – the AI learns from them how to use each tool.
"""

import logging
import os
import re
import subprocess
import time
from typing import Optional

import pyautogui
import pygetwindow
import pyperclip

from . import apps
from .screen import LiveScreen

log = logging.getLogger("pc-agent")

# Moving the mouse to the top-left corner = emergency stop (raises on the next action).
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.03

screen: LiveScreen = None  # set in main.py


class Paused(Exception):
    pass


class State:
    paused = False


def _guard(action: str):
    if State.paused:
        raise Paused("The user paused the AI. Wait until they resume it.")
    log.info(action)


# ---- protection against an AI tricked by text on a web page or in a file ----
# The app's own settings (API keys, the secret address) and the Cloudflare login are off limits,
# and any secret that still shows up in command output is hidden before the AI sees it.

PROTECTED = re.compile(
    r"pc[\s_-]*agent.{0,40}(config|\.json|\.log|appdata|roaming)"
    r"|(config|\.json|appdata|roaming).{0,40}pc[\s_-]*agent"
    r"|\.wrangler|xdg\.config|cloudflared",
    re.I | re.S,
)
BLOCKED = "Blocked: PC Agent's own settings and keys are protected. Never try to read them."

secrets_provider = lambda: []  # noqa: E731 – set in main.py, returns the current secret values


def touches_protected(text: str) -> bool:
    return bool(PROTECTED.search(text.replace("`", "").replace('"', "").replace("'", "")))


def hide_secrets(text: str) -> str:
    for value in secrets_provider():
        if value and len(value) >= 12:
            text = text.replace(value, "[hidden]")
    return text


def launch_app(name: str) -> str:
    """Launch an installed program or game by name in one step – the fastest way to open anything.
    Searches the Start menu, Steam and Epic Games. Examples: "Rocket League", "Total Commander",
    "Spotify", "Chrome", "Discord"."""
    _guard(f"launch_app {name}")
    app = apps.find(name)
    if app is None:
        return f'No installed app matches "{name}". Try list_apps or open_target.'
    os.startfile(app.target)
    changed = screen.wait_for_change(timeout=4)
    return f"Launched {app.name} ({app.source})." + ("" if changed else " It may still be loading.")


def list_apps(filter: str = "") -> str:
    """List installed programs and games (optionally only names containing `filter`)."""
    _guard(f"list_apps {filter}")
    names = sorted({f"{a.name} [{a.source}]" for a in apps.all_apps() if filter.lower() in a.name.lower()})
    return "\n".join(names[:200]) or "Nothing found."


def open_target(target: str) -> str:
    """Open a website, file, folder or protocol URL. Examples: "https://google.com/search?q=weather",
    "C:\\Users", "notepad", "steam://run/730". For installed apps prefer launch_app."""
    _guard(f"open {target}")
    if touches_protected(target):
        return BLOCKED
    subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=subprocess.CREATE_NO_WINDOW)
    changed = screen.wait_for_change(timeout=4)
    return "Opened." if changed else "Started, the screen has not changed yet."


def click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """Click a point on the screenshot. button: left | right | middle. clicks: 2 = double click."""
    _guard(f"click {x},{y} {button} x{clicks}")
    pyautogui.click(*screen.to_screen(x, y), clicks=clicks, interval=0.06, button=button)
    return f"Clicked ({x}, {y})."


def move_mouse(x: int, y: int) -> str:
    """Move the cursor to a point on the screenshot (e.g. to reveal a hover menu)."""
    _guard(f"move {x},{y}")
    pyautogui.moveTo(*screen.to_screen(x, y), duration=0.1)
    return "Mouse moved."


def drag(from_x: int, from_y: int, to_x: int, to_y: int) -> str:
    """Drag with the left mouse button from one point to another (files, windows, sliders)."""
    _guard(f"drag {from_x},{from_y} -> {to_x},{to_y}")
    pyautogui.moveTo(*screen.to_screen(from_x, from_y))
    pyautogui.dragTo(*screen.to_screen(to_x, to_y), duration=0.3, button="left")
    return "Dragged."


def scroll(amount: int, x: Optional[int] = None, y: Optional[int] = None) -> str:
    """Scroll the mouse wheel. Positive = up, negative = down (5 ≈ half a page).
    Optionally move the cursor to x, y first."""
    _guard(f"scroll {amount}")
    if x is not None and y is not None:
        pyautogui.moveTo(*screen.to_screen(x, y))
    pyautogui.scroll(amount * 120)
    return "Scrolled."


def type_text(text: str, press_enter: bool = False) -> str:
    """Type text into the focused field (any language/characters). Optionally press Enter after."""
    _guard(f"type {len(text)} chars")
    try:
        previous = pyperclip.paste()
    except Exception:
        previous = None
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.1)
    if previous is not None:
        pyperclip.copy(previous)
    if press_enter:
        pyautogui.press("enter")
    return "Typed."


def press_keys(keys: str, repeat: int = 1) -> str:
    """Press a key or shortcut, e.g. "enter", "esc", "tab", "ctrl+c", "alt+tab", "win+d", "f5".
    repeat = how many times in a row."""
    _guard(f"keys {keys} x{repeat}")
    combo = [k.strip().lower() for k in keys.split("+")]
    for _ in range(max(repeat, 1)):
        if len(combo) > 1:
            pyautogui.hotkey(*combo)
        else:
            pyautogui.press(combo[0])
    return f"Pressed {keys}."


def hold_key(key: str, hold_seconds: float) -> str:
    """Hold a key down for some time (e.g. moving in a game: "w" for 2 seconds). Max 30 s."""
    _guard(f"hold {key} {hold_seconds}s")
    pyautogui.keyDown(key)
    time.sleep(min(hold_seconds, 30))
    pyautogui.keyUp(key)
    return f"Held {key} for {hold_seconds} s."


def run_command(command: str, timeout_seconds: int = 60) -> str:
    """Run a PowerShell command and return its output (files, installs, system info...)."""
    _guard(f"run {command}")
    if touches_protected(command):
        return BLOCKED
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True, text=True, timeout=timeout_seconds,
        encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
    )
    out = (proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr.strip() else "")).strip()
    return hide_secrets(f"exit {proc.returncode}\n{out[-8000:]}")


def list_windows() -> str:
    """Titles of open windows – faster than a screenshot when looking for a program."""
    _guard("list_windows")
    titles = [w.title for w in pygetwindow.getAllWindows() if w.title.strip() and w.visible]
    return "\n".join(dict.fromkeys(titles)) or "No windows."


def focus_window(title_contains: str) -> str:
    """Bring to front the window whose title contains the text (e.g. "Chrome", "Total Commander")."""
    _guard(f"focus {title_contains}")
    matches = [w for w in pygetwindow.getAllWindows() if title_contains.lower() in w.title.lower()]
    if not matches:
        return f'No window title contains "{title_contains}".'
    win = matches[0]
    if win.isMinimized:
        win.restore()
    # Windows refuses to switch focus without a key press – Alt works around it.
    pyautogui.press("alt")
    win.activate()
    time.sleep(0.2)
    return f"Focused: {win.title}"


def wait_for_change(timeout_seconds: float = 10) -> str:
    """Wait until the screen changes and settles (page load, window opening, game turn)."""
    _guard(f"wait_for_change {timeout_seconds}s")
    changed = screen.wait_for_change(timeout=min(timeout_seconds, 120))
    return "The screen changed." if changed else "No change (timeout)."


def screenshot() -> str:
    """Get a fresh screenshot (instant – the app always has one ready)."""
    _guard("screenshot")
    return "Screenshot attached."


def recent_activity(seconds: float = 30) -> str:
    """What happened on the screen in the last seconds (without sending an image)."""
    _guard("recent_activity")
    return screen.activity_summary(seconds)


def wait(seconds: float) -> str:
    """Wait for some time (e.g. while something installs or downloads). Max 120 s."""
    _guard(f"wait {seconds}s")
    time.sleep(min(seconds, 120))
    return f"Waited {seconds} s."


ALL = [
    launch_app, list_apps, open_target, click, move_mouse, drag, scroll, type_text, press_keys,
    hold_key, run_command, list_windows, focus_window, wait_for_change, screenshot, recent_activity, wait,
]

# The MCP server attaches a screenshot to the result of these actions.
WITH_IMAGE = {"screenshot", "launch_app", "open_target", "focus_window", "wait_for_change", "wait"}
