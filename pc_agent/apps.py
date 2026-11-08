"""Index of installed programs and games (Start menu, Steam, Epic) so the AI can launch
anything with a single call instead of clicking through menus."""

import difflib
import json
import os
import re
import subprocess
import time
import winreg
from dataclasses import dataclass
from pathlib import Path


@dataclass
class App:
    name: str
    source: str  # "Start menu" | "Windows" | "Steam" | "Epic"
    target: str  # path or URL to open
    alias: str = ""  # English name for built-in apps whose display name is localized


_cache: tuple[float, list[App]] = (0.0, [])


def _start_menu() -> list[App]:
    roots = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
        Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs",
    ]
    apps = []
    for root in roots:
        for lnk in root.rglob("*.lnk"):
            if not re.search(r"uninstall|odinštal|readme|help|website", lnk.stem, re.I):
                apps.append(App(lnk.stem, "Start menu", str(lnk)))
    return apps


def _windows_apps() -> list[App]:
    """Everything in the Start menu's app list, including built-in/Store apps (Notepad, Settings...)."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-StartApps | ForEach-Object { $_.Name + [char]9 + $_.AppID }"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    apps = []
    for line in out.splitlines():
        name, _, app_id = line.partition("\t")
        if name and app_id and not re.search(r"uninstall|odinštal", name, re.I):
            app_id = app_id.strip()
            apps.append(App(name.strip(), "Windows", f"shell:AppsFolder\\{app_id}", _alias(app_id)))
    return apps


# Built-in apps whose ID does not contain their English name.
KNOWN_ALIASES = {"immersivecontrolpanel": "settings", "photos": "photos", "windowsterminal": "terminal"}


def _alias(app_id: str) -> str:
    """"Microsoft.WindowsCalculator_8wekyb3d8bbwe!App" -> "calculator" (display names may be localized)."""
    if "!" not in app_id:  # only Store/built-in packages; programs and browser web-apps keep their name
        return ""
    base = re.split(r"[_!]", app_id)[0].lower()
    base = re.sub(r"^(microsoft\.|windows\.)+", "", base)
    base = re.sub(r"^windows(?=[a-z])", "", base)
    return KNOWN_ALIASES.get(base, base)


def _steam() -> list[App]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return []
    libraries = {steam}
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        for path in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore")):
            libraries.add(Path(path.replace("\\\\", "\\")))
    apps = []
    for lib in libraries:
        for manifest in (lib / "steamapps").glob("appmanifest_*.acf"):
            text = manifest.read_text(encoding="utf-8", errors="ignore")
            appid, name = re.search(r'"appid"\s+"(\d+)"', text), re.search(r'"name"\s+"([^"]+)"', text)
            if appid and name and "Steamworks" not in name.group(1):
                apps.append(App(name.group(1), "Steam", f"steam://rungameid/{appid.group(1)}"))
    return apps


def _epic() -> list[App]:
    folder = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Epic\EpicGamesLauncher\Data\Manifests"
    apps = []
    for item in folder.glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("bIsApplication", True) and not data.get("AppName", "").startswith(("UE_", "Fab", "Quixel")):
            uri = f"{data['CatalogNamespace']}%3A{data['CatalogItemId']}%3A{data['AppName']}"
            name = data["DisplayName"].replace("®", "").replace("™", "").strip()
            apps.append(App(name, "Epic", f"com.epicgames.launcher://apps/{uri}?action=launch&silent=true"))
    return apps


def all_apps() -> list[App]:
    global _cache
    if time.time() - _cache[0] > 120:
        apps = []
        for source in (_epic, _steam, _start_menu, _windows_apps):
            try:
                apps += source()
            except Exception:
                pass
        _cache = (time.time(), apps)
    return _cache[1]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _score(q: str, n: str) -> float:
    if not n:
        return 0.0
    if n == q:
        return 3.0
    if n.startswith(q) or q.startswith(n):
        return 2.0
    if q in n:
        return 1.5
    return difflib.SequenceMatcher(None, q, n).ratio()


def find(query: str) -> App | None:
    q = _norm(query)
    best, best_score = None, 0.0
    for app in all_apps():
        score = max(_score(q, _norm(app.name)), _score(q, _norm(app.alias)))
        # Games from launchers win over a same-named Start menu shortcut.
        score += 0.1 if app.source in ("Steam", "Epic") else 0
        if score > best_score:
            best, best_score = app, score
    # Fuzzy matches below 0.8 are usually a different app (e.g. "spotify" vs "spustiť").
    return best if best_score >= 0.8 else None
