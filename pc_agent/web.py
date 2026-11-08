"""App UI (chat + live view + settings) – the same page on the PC and on the phone.

Everything lives under /app/<secret>, so nobody without the secret can reach the app.
"""

import io
import os
import secrets
import threading
from pathlib import Path

import pyperclip
import qrcode
import qrcode.image.svg
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from . import actions, android, autostart, installer
from .agent import Agent, enabled_providers
from .config import save_config
from .llm import PROVIDERS, detect_provider, find_local_ai, validate_key

UI_HTML = (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")


def svg_qr(text: str) -> Response:
    buf = io.BytesIO()
    qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


def register_routes(mcp, cfg: dict, agent: Agent, urls: dict, tunnel, on_show=None, on_quit=None):
    def authorized(request: Request) -> bool:
        return secrets.compare_digest(request.path_params.get("token", ""), cfg["secret"])

    def route(path: str, methods=("GET",)):
        """Decorator: registers a path under /app/{token} and checks the secret."""

        def deco(handler):
            async def guarded(request: Request):
                if not authorized(request):
                    return Response("Not found", status_code=404)
                return await handler(request)

            mcp.custom_route("/app/{token}" + path, methods=list(methods), include_in_schema=False)(guarded)
            return handler

        return deco

    def links() -> dict:
        # The relay's address never changes; the tunnel's own one is the fallback.
        remote = tunnel.stable_base or (f"https://{tunnel.host}" if tunnel.host else None)
        return {
            "lan": urls.get("mobile"),
            "remote": f"{remote}/app/{cfg['secret']}" if remote else None,
            "mcp": f"{remote}/mcp/{cfg['secret']}" if remote else None,
            "stable": bool(tunnel.stable_base),
            "remote_status": tunnel.status,
            "remote_error": tunnel.error,
        }

    @route("")
    async def index(request: Request):
        return HTMLResponse(UI_HTML)

    @route("/frame.jpg")
    async def frame(request: Request):
        try:
            data = await run_in_threadpool(agent.screen.jpeg)
        except android.PhoneError:
            agent.set_target("pc")
            data = await run_in_threadpool(actions.screen.jpeg)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def phone_info() -> dict:
        p = android.phone
        return {"status": p.status, "model": p.model, "address": cfg.get("phone_address", ""),
                "qr_state": p.qr_state, "paired_before": bool(cfg.get("phone_paired")),
                "has_pin": bool(cfg.get("phone_pin")), "pin_failed": p.pin_failed}

    @route("/api/state")
    async def state(request: Request):
        since = int(request.query_params.get("since", 0))
        return JSONResponse({
            "events": agent.events_since(since),
            "running": agent.running,
            "paused": actions.State.paused,
            "has_key": bool(enabled_providers(cfg)),
            "target": agent.target,
            "phone": phone_info(),
        })

    @route("/api/target", methods=("POST",))
    async def target(request: Request):
        wanted = (await request.json()).get("target")
        if agent.running:
            return JSONResponse({"ok": False, "error": "Stop the running task first."})
        if wanted == "phone" and not android.phone.connected:
            return JSONResponse({"ok": False, "error": "Connect a phone first (Settings, Android phone)."})
        agent.set_target("phone" if wanted == "phone" else "pc")
        return JSONResponse({"ok": True, "target": agent.target})

    async def phone_call(fn, *args) -> JSONResponse:
        try:
            message = await run_in_threadpool(fn, *args)
            save_config(cfg)
            return JSONResponse({"ok": True, "message": message, **phone_info()})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e), **phone_info()})

    @route("/api/phone/pair", methods=("POST",))
    async def phone_pair(request: Request):
        body = await request.json()
        return await phone_call(android.phone.pair, body.get("address", ""), body.get("code", ""))

    @route("/api/phone/connect", methods=("POST",))
    async def phone_connect(request: Request):
        return await phone_call(android.phone.connect, (await request.json()).get("address", ""))

    @route("/api/phone/disconnect", methods=("POST",))
    async def phone_disconnect(request: Request):
        agent.set_target("pc")
        return await phone_call(android.phone.disconnect)

    @route("/api/phone/pin", methods=("POST",))
    async def phone_pin(request: Request):
        # Only the phone's own unlock code; saved encrypted, typed by the app, never shown to the AI.
        pin = str((await request.json()).get("pin", "")).strip()
        if pin and (len(pin) > 32 or not all(32 < ord(ch) < 127 for ch in pin)):
            return JSONResponse({"ok": False, "error": "Use digits, or English letters and symbols, without spaces.", **phone_info()})
        if pin:
            cfg["phone_pin"] = pin
        else:
            cfg.pop("phone_pin", None)
        android.phone.pin_failed = False
        save_config(cfg)
        return JSONResponse({"ok": True, **phone_info()})

    @route("/api/phone/qr-start", methods=("POST",))
    async def phone_qr_start(request: Request):
        android.phone.start_qr_pairing()
        return JSONResponse({"ok": True})

    @route("/phone-qr.svg")
    async def phone_qr(request: Request):
        if not android.phone.qr_name:
            return Response("Not started", status_code=404)
        return svg_qr(android.phone.qr_text())

    async def do_install() -> JSONResponse:
        if not installer.can_install() and not installer.is_installed():
            return JSONResponse({"ok": True})  # portable copy or running from source
        try:
            exe = await run_in_threadpool(installer.install)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        if on_quit and not installer.is_installed():
            # Switch over to the installed copy so this folder is no longer needed.
            installer.restart_installed(exe)
            on_quit()
        return JSONResponse({"ok": True, "path": str(exe)})

    @route("/api/install", methods=("POST",))
    async def install(request: Request):
        return await do_install()

    @route("/api/get-started", methods=("POST",))
    async def get_started(request: Request):
        """The one button on the welcome screen: start with Windows (if ticked) and install."""
        if installer.is_frozen():
            enabled = bool((await request.json()).get("autostart"))
            autostart.set_enabled(enabled)
            cfg["autostart"] = enabled
            save_config(cfg)
        return await do_install() if installer.can_install() else JSONResponse({"ok": True})

    # ---- connecting an AI: one key field / the clipboard, the app works out the rest ----
    def add_key(kind: str, key: str):
        validate_key(kind, key)
        next(p for p in cfg["providers"] if p["type"] == kind).update(key=key, enabled=True)
        save_config(cfg)
        agent.settings_changed()

    @route("/api/key", methods=("POST",))
    async def set_key(request: Request):
        key = (await request.json()).get("key", "").strip()
        # A key format we do not recognise is most likely Gemini, the AI the app suggests.
        kind = detect_provider(key) or "gemini"
        try:
            await run_in_threadpool(add_key, kind, key)
        except Exception:
            return JSONResponse({"ok": False, "error": "That key did not work. Copy it again and retry."})
        return JSONResponse({"ok": True, "name": PROVIDERS[kind]["name"]})

    @route("/api/clipboard-key")
    async def clipboard_key(request: Request):
        """Called only while a "copy your key" screen is open on this PC."""
        if request.url.hostname not in ("127.0.0.1", "localhost"):
            return JSONResponse({"found": False})
        text = (await run_in_threadpool(pyperclip.paste) or "").strip()
        kind = detect_provider(text)
        if not kind or any(p.get("key") == text for p in cfg["providers"]):
            return JSONResponse({"found": False})
        try:
            await run_in_threadpool(add_key, kind, text)
        except Exception:
            return JSONResponse({"found": False, "error": "The copied key did not work."})
        return JSONResponse({"found": True, "name": PROVIDERS[kind]["name"]})

    @route("/api/local-ai", methods=("POST",))
    async def local_ai(request: Request):
        """Find LM Studio or Ollama on this PC and connect it as the last AI in the list."""
        found = await run_in_threadpool(find_local_ai)
        if not found:
            return JSONResponse({"ok": False, "error": "No local AI is running. Start LM Studio (local server on) "
                                                       "or Ollama with a model that can see images, then try again."})
        if not found["vision_models"]:
            return JSONResponse({"ok": False, "error": f"{found['name']} is running, but no loaded model can see "
                                                       "images. Load one such as Qwen2.5-VL or Gemma 3."})
        local = next(p for p in cfg["providers"] if p["type"] == "local")
        local.update(base_url=found["base_url"], model=found["vision_models"][0], enabled=True)
        save_config(cfg)
        agent.settings_changed()
        return JSONResponse({"ok": True, "name": f"{found['name']} ({local['model']})"})

    @route("/api/open-key-page", methods=("POST",))
    async def open_key_page(request: Request):
        info = PROVIDERS.get((await request.json()).get("type", "gemini"))
        if info and request.url.hostname in ("127.0.0.1", "localhost"):
            os.startfile(info["key_url"])
        return JSONResponse({"ok": bool(info), "url": info and info["key_url"]})

    @route("/api/send", methods=("POST",))
    async def send(request: Request):
        text = (await request.json()).get("text", "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "Empty message."})
        if not agent.submit(text):
            return JSONResponse({"ok": False, "error": "The AI is still working on the previous task."})
        return JSONResponse({"ok": True})

    @route("/api/stop", methods=("POST",))
    async def stop(request: Request):
        agent.stop()
        return JSONResponse({"ok": True})

    @route("/api/pause", methods=("POST",))
    async def pause(request: Request):
        actions.State.paused = not actions.State.paused
        agent.emit("status", "AI paused." if actions.State.paused else "AI resumed.")
        return JSONResponse({"paused": actions.State.paused})

    @route("/api/reset", methods=("POST",))
    async def reset(request: Request):
        if agent.running:
            return JSONResponse({"ok": False, "error": "Stop the running task first."})
        agent.reset()
        return JSONResponse({"ok": True})

    @route("/api/show", methods=("POST",))
    async def show(request: Request):
        # A second launch of the app asks the running one to show its window. Answer at once –
        # the first show of a window can take seconds.
        if on_show:
            threading.Thread(target=on_show, daemon=True).start()
        return JSONResponse({"ok": True})

    @route("/api/settings")
    async def get_settings(request: Request):
        providers = []
        for p in cfg["providers"]:
            info = PROVIDERS.get(p["type"], {})
            key = p.get("key") or ""
            connected = bool(key) or (p["type"] == "local" and bool(p.get("base_url")))
            providers.append({
                "type": p["type"], "name": info.get("name", p["type"]), "free": info.get("free", False),
                "key_url": info.get("key_url"), "has_key": connected, "key_hint": f"…{key[-4:]}" if key else "",
                "model": p.get("model", ""), "enabled": p.get("enabled", True),
            })
        return JSONResponse({
            "providers": providers,
            "mode": cfg.get("mode", "fast"),
            "autostart": cfg.get("autostart", False),
            "remote_access": cfg.get("remote_access", False),
            "can_install": installer.can_install(),
            "installed": installer.is_installed(),
            "phone": phone_info(),
            **links(),
        })

    @route("/api/settings", methods=("POST",))
    async def set_settings(request: Request):
        body = await request.json()
        if isinstance(body.get("providers"), list):
            by_type = {p["type"]: p for p in cfg["providers"]}
            ordered = []
            for item in body["providers"]:
                p = by_type.get(item.get("type"))
                if not p:
                    continue
                if item.get("key"):
                    p["key"] = item["key"].strip()
                if item.get("clear_key"):
                    p["key"] = ""
                    if p["type"] == "local":
                        p["base_url"] = ""
                p["model"] = (item.get("model") or "").strip()
                p["enabled"] = bool(item.get("enabled", True))
                ordered.append(p)
            cfg["providers"] = ordered + [p for p in cfg["providers"] if p not in ordered]
        if body.get("mode") in ("fast", "smart"):
            cfg["mode"] = body["mode"]
        if "autostart" in body and bool(body["autostart"]) != cfg.get("autostart"):
            autostart.set_enabled(bool(body["autostart"]))
            cfg["autostart"] = bool(body["autostart"])
        save_config(cfg)
        agent.settings_changed()
        return JSONResponse({"ok": True})

    @route("/api/remote", methods=("POST",))
    async def remote(request: Request):
        enabled = bool((await request.json()).get("enabled"))
        cfg["remote_access"] = enabled
        save_config(cfg)
        tunnel.start() if enabled else tunnel.stop()
        return JSONResponse({"ok": True, **links()})

    @route("/api/links")
    async def get_links(request: Request):
        return JSONResponse(links())

    @route("/qr.svg")
    async def qr(request: Request):
        target = links().get(request.query_params.get("for", "lan"))
        if not target:
            return Response("Not available", status_code=404)
        return svg_qr(target)
