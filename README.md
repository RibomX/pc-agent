# PC Agent

Let a **free** AI use your Windows PC – and your Android phone – for you. Type what you want
("Open YouTube and play relaxing music", "Launch Rocket League") and watch it happen on a live view
of the screen, on the PC or from your phone.

Made for people who do not want to pay for an AI subscription to get an AI that can use their
computer. It runs on free AI keys (Google Gemini, OpenRouter, Groq), on Claude / OpenAI keys if you
have them, or on a local model (LM Studio or Ollama), and switches to the next one when a free daily
limit runs out.

> **Use at your own risk.** PC Agent lets an AI click, type and run commands on your computer
> **without asking first**. AI makes mistakes and can be tricked by text it reads on web pages.
> Do not use it on a computer with things you cannot afford to lose, and close banking, passwords
> and other private things while it works. This is a hobby project without any warranty – see
> [LICENSE](LICENSE).

## What it can do

- **Control the PC:** open programs and games (Start menu, Steam, Epic), click, type, scroll,
  run PowerShell commands, switch windows.
- **Control an Android phone** (Android 11+) through wireless debugging – no root, no app on the phone.
  A phone with the screen off is woken up, and a locked one is unlocked with the phone password you
  saved (the AI never sees it and cannot type anything else on the lock screen).
- **Use it from your phone:** the same chat and live screen in the phone's browser, on the same Wi-Fi
  or from anywhere through a free Cloudflare tunnel.
- **Use it from another AI app:** it is also an MCP server, so Claude (also on the free plan, as a
  custom connector) or Gemini can control your PC through it.

## Getting started

1. Download `PC Agent` from the [Releases](../../releases) page and run `PC Agent.exe`.
   Windows may warn that the app is unknown (it is not code-signed): **More info → Run anyway**.
2. Press **Get started** – the app installs itself (Start menu + Desktop shortcut).
3. Press **Open Google**, then **Create API key** and **Copy**. PC Agent finds the key by itself.
4. Type what the AI should do.

**Portable version:** download `PC-Agent-Portable.zip`, unzip it anywhere (also a USB stick) and run
`PC Agent.exe`. Nothing is installed: settings stay in the `data` folder next to the exe (delete the
folder to remove everything). It is the `portable.txt` file that makes a copy portable.

Closing the window hides PC Agent next to the clock. Settings (gear icon):

- **Use on your phone** – scan the QR code. *Also work away from home* turns on a free Cloudflare tunnel.
- **Let the AI use your Android phone** – step-by-step setup, then a QR code to scan on the phone.
- **AI** – add more free keys for more uses per day; the app recognises which AI a key belongs to.
  **Find local AI** connects a running LM Studio (local server on) or Ollama with a model that can
  see images (e.g. Qwen2.5-VL, Gemma 3) – free, unlimited and offline, but it needs a strong GPU.
- **Advanced** – speed, order of AIs, models, the MCP connector address, pairing a phone by code,
  and **Phone password** (PIN or password used to unlock the phone; pattern locks are not supported).

## Privacy

- While the AI works, **pictures of your screen are sent to the AI service** you connected.
  Free AI services may use them to improve their products (per Google's terms, not in the
  EU/EEA, UK and Switzerland). OpenRouter's free models may be run by providers that train on data.
- Screenshots are kept only in memory: capturing stops after 10 s when nobody needs it, and all
  screenshots are removed from the conversation after each task. Nothing is saved to disk.
- While the "copy your key" screen is open, PC Agent reads the PC's clipboard to find the key.

## Security

- API keys, the phone password and the app's secret address are stored encrypted (Windows DPAPI).
  If the saved phone password is wrong, it is tried only once, so the phone does not lock you out.
- The app's address (also in QR codes and the connector address) contains a secret: whoever has it
  controls your PC. To make a new one: quit the app, delete the `secret` line in
  `%APPDATA%\PC Agent\config.json` and start the app again.
- The AI is told that text on screen is never an instruction, commands touching PC Agent's own
  settings or the Cloudflare login are blocked, and secrets are hidden from command output.
  This lowers the risk of the AI being manipulated; it does not remove it.
- **STOP** ends a task, **Ctrl+Alt+P** pauses everything (PC and phone), moving the mouse to the
  top-left corner stops the current action.

## Known limits

- Free AI is slow for games and fiddly menus; fast games are out of reach.
- Games in exclusive fullscreen are invisible to screenshots – set them to *Borderless*.
- Typing on the phone supports English characters only (an Android limitation). iPhones are not supported.
- The away-from-home address changes when the app restarts, unless you run your own relay (below).

## Building from source

Requires Windows and Python 3.10+.

```bat
start.bat     :: run from source (creates .venv and installs requirements.txt)
build.bat     :: build release\PC Agent\PC Agent.exe with PyInstaller
```

`python run.py --no-window` runs only the server (open the printed address in a browser).

Code map: `agent.py` (task loop), `llm.py` (AI providers), `actions.py` + `apps.py` (PC control),
`android.py` (phone), `screen.py` (screen capture), `tools.py` (MCP server), `web.py` + `ui.html`
(the app's UI), `tunnel.py` (remote access), `config.py` (encrypted settings), `installer.py`,
`autostart.py`, `verify.py` (signature checks of downloaded tools).

### Optional: your own relay for a permanent address

`relay/worker.js` is a small Cloudflare Worker (free plan) that gives each PC an address that never
changes, so phone shortcuts and AI connectors keep working after restarts.

1. Copy `relay/wrangler.toml.example` to `relay/wrangler.toml`.
2. `npx wrangler login`, then `npx wrangler kv namespace create DEVICES` and put the id into `wrangler.toml`.
3. `npx wrangler deploy`, then `npx wrangler logout` (do not leave the login on a PC an AI controls).
4. Put the Worker's address into `relay_url` in `%APPDATA%\PC Agent\config.json` and restart the app.

## License

MIT – see [LICENSE](LICENSE).
