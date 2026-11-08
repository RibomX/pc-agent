"""AI providers behind one interface, so the agent loop works the same with any of them.

Gemini (native SDK, free tier), Claude (Anthropic SDK), and everything that speaks the OpenAI
chat format: OpenRouter (free models), Groq (free tier), OpenAI, or a custom endpoint.
A provider raises ProviderError when it cannot continue (no key, quota used up, broken) –
the agent then moves the task to the next provider in the user's list.
"""

import base64
import datetime
import inspect
import json
import logging
import re
import time
import typing
from dataclasses import dataclass, field

log = logging.getLogger("pc-agent")

REQUEST_TIMEOUT_S = 45
FAST_TIMEOUT_S = 12
KEEP_IMAGES = 2

# Catalog shown in Settings. "free" = usable without paying.
PROVIDERS = {
    "gemini": {"name": "Google Gemini", "free": True, "key_url": "https://aistudio.google.com/apikey"},
    "openrouter": {"name": "OpenRouter", "free": True, "key_url": "https://openrouter.ai/settings/keys",
                   "base_url": "https://openrouter.ai/api/v1"},
    "groq": {"name": "Groq", "free": True, "key_url": "https://console.groq.com/keys",
             "base_url": "https://api.groq.com/openai/v1"},
    "anthropic": {"name": "Claude (Anthropic)", "free": False, "key_url": "https://platform.claude.com/settings/keys"},
    "openai": {"name": "OpenAI", "free": False, "key_url": "https://platform.openai.com/api-keys",
               "base_url": "https://api.openai.com/v1"},
    # A model running on this PC (LM Studio or Ollama) – free, unlimited, offline; needs a strong GPU.
    "local": {"name": "Local AI", "free": True, "key_url": "https://lmstudio.ai"},
}

# Where LM Studio and Ollama serve their OpenAI-compatible API by default.
LOCAL_SERVERS = {"LM Studio": "http://127.0.0.1:1234/v1", "Ollama": "http://127.0.0.1:11434/v1"}
# Model names that can see images (PC Agent needs to see the screen), most capable families first.
VISION_MODEL = re.compile(r"qwen[\d.]*-?vl|gemma-?3|llava|llama-?3\.2-?vision|minicpm-?v|pixtral|mistral-small-3|"
                          r"internvl|molmo|vision|-vl\b|\bvl\b", re.I)
LOCAL_TIMEOUT_S = 300  # local models on a home PC can be slow


def find_local_ai() -> dict | None:
    """Look for a running LM Studio or Ollama. Returns {"name", "base_url", "models", "vision_models"}."""
    import urllib.request

    for name, base_url in LOCAL_SERVERS.items():
        try:
            with urllib.request.urlopen(f"{base_url}/models", timeout=2) as r:
                models = [m["id"] for m in json.load(r).get("data", [])]
        except Exception:
            continue
        return {"name": name, "base_url": base_url, "models": models,
                "vision_models": [m for m in models if VISION_MODEL.search(m)]}
    return None


class ProviderError(Exception):
    """This provider cannot continue the task – try the next one."""


# How each provider's keys look, so the user never has to say which AI a key belongs to.
# The generic OpenAI pattern must stay last.
KEY_PATTERNS = (
    ("anthropic", r"sk-ant-[\w-]{20,}"),
    ("openrouter", r"sk-or-[\w-]{20,}"),
    ("groq", r"gsk_\w{20,}"),
    ("gemini", r"(AIza|AQ\.)[\w.-]{30,}"),  # AI Studio now issues AQ. keys, older ones start with AIza
    ("openai", r"sk-[\w-]{20,}"),
)


def detect_provider(key: str) -> str | None:
    key = key.strip()
    return next((kind for kind, pattern in KEY_PATTERNS if re.fullmatch(pattern, key)), None)


def validate_key(kind: str, key: str):
    """Raises if the provider rejects the key (listing models is free and uses no quota)."""
    if kind == "gemini":
        from google import genai

        client = genai.Client(api_key=key)  # must stay referenced, it closes itself when collected
        next(iter(client.models.list()))
    elif kind == "anthropic":
        import anthropic

        anthropic.Anthropic(api_key=key, timeout=20.0).models.list()
    else:
        import openai

        openai.OpenAI(api_key=key, base_url=PROVIDERS[kind]["base_url"], timeout=20).models.list()


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Reply:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)


# ---------------------------------------------------------------- tool schemas

_JSON_TYPES = {int: "integer", float: "number", str: "string", bool: "boolean"}


def tool_schema(fn) -> dict:
    """JSON schema of a Python action, built from its signature and docstring."""
    props, required = {}, []
    for name, p in inspect.signature(fn).parameters.items():
        ann = p.annotation
        if typing.get_origin(ann) is typing.Union:  # Optional[int] -> int
            ann = next(a for a in typing.get_args(ann) if a is not type(None))
        props[name] = {"type": _JSON_TYPES.get(ann, "string")}
        if p.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "name": fn.__name__,
        "description": inspect.cleandoc(fn.__doc__ or ""),
        "parameters": {"type": "object", "properties": props, "required": required},
    }


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------- base


class Backend:
    """One AI provider. The agent calls start() once per task, then step()/add_results() in a loop."""

    coords = "pixels"  # or "norm1000" – how the model expresses x/y

    def __init__(self, entry: dict, tools: list, system_prompt: str, emit, stop_event):
        self.entry = entry
        self.tools = tools
        self.system_prompt = system_prompt
        self.emit = emit
        self.stop = stop_event
        self.model: str | None = None
        if not entry.get("key") and entry["type"] not in ("custom", "local"):
            raise ProviderError(f"{self.label}: no API key.")

    @property
    def label(self) -> str:
        return PROVIDERS.get(self.entry["type"], {}).get("name", self.entry["type"])

    def start(self, text: str, image: bytes):
        raise NotImplementedError

    def step(self) -> Reply:
        raise NotImplementedError

    def add_results(self, results: list[tuple[ToolCall, str]], image: bytes, notes: list[str]):
        raise NotImplementedError

    def boost(self) -> bool:
        """Use a stronger model for the next few steps. Returns False if there is none."""
        return False

    def forget_images(self) -> bool:
        """Remove every screenshot from the conversation (privacy, after a task). False if this provider
        cannot do that without breaking the conversation – the agent then starts a new one."""
        return False


# ---------------------------------------------------------------- Gemini


NOT_CHAT_MODELS = (
    "image", "tts", "audio", "embedding", "live", "omni", "robotics", "transcribe", "computer-use",
    "antigravity", "deep-research", "nano-banana", "lyria", "customtools",
)
BOOST_STEPS = 4
_gemini_models: dict[str, list[str]] = {}  # key -> model list (cached for the app's lifetime)
_exhausted: dict[str, datetime.date] = {}  # model -> day its daily quota ran out


class GeminiBackend(Backend):
    coords = "norm1000"  # Gemini points on a 0-1000 grid

    def __init__(self, *a, fast: bool = True, **kw):
        super().__init__(*a, **kw)
        from google import genai
        from google.genai import types

        self.genai, self.types = genai, types
        self.client = genai.Client(api_key=self.entry["key"], http_options=types.HttpOptions(
            timeout=REQUEST_TIMEOUT_S * 1000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.fast = fast
        self.contents = []
        self.boost_steps = 0
        self.declarations = [
            types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"])
            for t in self.tools
        ]

    def _models(self) -> list[str]:
        key = self.entry["key"]
        if key not in _gemini_models:
            names = []
            for m in self.client.models.list():
                name = m.name.removeprefix("models/")
                if "generateContent" in (m.supported_actions or []) and not any(x in name for x in NOT_CHAT_MODELS):
                    names.append(name)
            _gemini_models[key] = names
        return _gemini_models[key]

    def candidates(self, strong: bool = False) -> list[str]:
        today = datetime.date.today()

        def rank(n: str):
            unstable = any(x in n for x in ("preview", "exp", "latest"))
            lite = "lite" in n
            group = 2 if "flash" not in n else (0 if lite == self.fast else 1)
            return (group, unstable, [-ord(c) for c in n])

        models = [n for n in self._models() if _exhausted.get(n) != today]
        if strong:
            models = [n for n in models if "flash" in n and "lite" not in n]
        ordered = sorted(models, key=rank)
        chosen = self.entry.get("model")
        if chosen and not strong:
            ordered = [chosen] + [n for n in ordered if n != chosen]
        if self.model in ordered and not strong:
            ordered = [self.model] + [n for n in ordered if n != self.model]
        return ordered

    def boost(self) -> bool:
        if self.candidates(strong=True):
            self.boost_steps = BOOST_STEPS
            return True
        return False

    def _config(self, model: str):
        t = self.types
        lite = "lite" in model
        extra = {}
        if model.startswith("gemini-3"):
            extra["thinking_config"] = t.ThinkingConfig(thinking_level="MINIMAL" if lite else "LOW")
        # Flash-Lite normally answers in 1–3 s; a request stuck on a busy free server is retried sooner.
        timeout = (FAST_TIMEOUT_S if lite else REQUEST_TIMEOUT_S) * 1000
        return t.GenerateContentConfig(
            http_options=t.HttpOptions(timeout=timeout, retry_options=t.HttpRetryOptions(attempts=1)),
            system_instruction=self.system_prompt,
            tools=[t.Tool(function_declarations=self.declarations)],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            **extra,
        )

    def _image(self, image: bytes):
        return self.types.Part.from_bytes(data=image, mime_type="image/jpeg")

    def start(self, text: str, image: bytes):
        self.contents.append(self.types.Content(role="user", parts=[self.types.Part.from_text(text=text), self._image(image)]))

    def add_results(self, results, image, notes):
        t = self.types
        parts = [t.Part.from_function_response(name=c.name, response={"result": r}) for c, r in results]
        parts += [t.Part.from_text(text=n) for n in notes]
        parts.append(self._image(image))
        self.contents.append(t.Content(role="user", parts=parts))

    def _prune(self, keep: int = KEEP_IMAGES):
        seen = 0
        for content in reversed(self.contents):
            for i, part in enumerate(content.parts or []):
                if part.inline_data is not None:
                    seen += 1
                    if seen > keep:
                        content.parts[i] = self.types.Part.from_text(text="[older screenshot]")

    def forget_images(self) -> bool:
        self._prune(keep=0)
        return True

    def step(self) -> Reply:
        import httpx

        self._prune()
        strong = self.boost_steps > 0
        if strong:
            self.boost_steps -= 1
        models = self.candidates(strong=strong) or self.candidates()
        if not models:
            raise ProviderError("Gemini: all free models reached today's limit.")
        waits, timed_out = 0, set()
        while models:
            model = models[0]
            try:
                t0 = time.time()
                response = self.client.models.generate_content(model=model, contents=self.contents, config=self._config(model))
                log.info(f"{model} answered in {time.time() - t0:.1f} s")
                if not strong and self.model != model:
                    self.model = model
                    self.emit("status", f"Using {self.label} ({model}).")
                break
            except httpx.TimeoutException:
                if model not in timed_out and not self.stop.is_set():
                    timed_out.add(model)  # one slow answer is usually a busy server – retry once
                    self.emit("status", "The AI is slow right now, retrying…")
                    continue
                models.pop(0)
            except self.genai.errors.APIError as e:
                msg = str(e)
                log.warning(f"{model}: {e.code} {(e.message or msg)[:200]}")
                if e.code in (401, 403) and "API key" in msg:
                    raise ProviderError("Gemini: the API key is not valid.")
                if e.code == 429 and "PerDay" in msg:
                    _exhausted[model] = datetime.date.today()
                retry = re.search(r"retry in ([\d.]+)s", msg)
                if e.code == 429 and retry and float(retry.group(1)) < 90 and "PerDay" not in msg and waits < 3:
                    waits += 1
                    self.emit("status", f"Per-minute AI limit, waiting {float(retry.group(1)):.0f} s…")
                    time.sleep(float(retry.group(1)) + 1)
                    continue
                models.pop(0)
            if models and not self.stop.is_set():
                self.emit("status", f"{model} is unavailable, trying {models[0]}…")
        else:
            raise ProviderError("Gemini: no model answered (daily limits or server problems).")

        candidate = response.candidates[0] if response.candidates else None
        if candidate is None or candidate.content is None:
            raise ProviderError("Gemini returned no answer (maybe blocked by a filter).")
        self.contents.append(candidate.content)
        parts = candidate.content.parts or []
        return Reply(
            text="".join(p.text for p in parts if p.text and not p.thought).strip(),
            calls=[ToolCall(str(i), p.function_call.name, dict(p.function_call.args or {}))
                   for i, p in enumerate(parts) if p.function_call],
        )


# ---------------------------------------------------------------- OpenAI-compatible


# Preferred free OpenRouter vision models, most capable first (matched as substrings).
OPENROUTER_PREFERENCE = ("gemini", "qwen", "llama-4", "mistral", "gemma")


class OpenAIBackend(Backend):
    """OpenRouter, Groq, OpenAI and custom endpoints (LM Studio, Ollama...) – the OpenAI chat format."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        import openai

        self.openai = openai
        base_url = self.entry.get("base_url") or PROVIDERS.get(self.entry["type"], {}).get("base_url")
        if not base_url:
            raise ProviderError(f"{self.label}: no address set.")
        timeout = LOCAL_TIMEOUT_S if self.entry["type"] == "local" else REQUEST_TIMEOUT_S
        self.client = openai.OpenAI(api_key=self.entry.get("key") or "none", base_url=base_url,
                                    timeout=timeout, max_retries=1)
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.tool_defs = [{"type": "function", "function": t} for t in self.tools]
        self.models = self._pick_models()

    def _pick_models(self) -> list[str]:
        if self.entry.get("model"):
            return [self.entry["model"]]
        kind = self.entry["type"]
        try:
            if kind == "openrouter":
                import urllib.request

                with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=15) as r:
                    data = json.load(r)["data"]
                free = [m["id"] for m in data
                        if m["id"].endswith(":free")
                        and "image" in (m.get("architecture", {}).get("input_modalities") or [])
                        and "tools" in (m.get("supported_parameters") or [])]
                return sorted(free, key=lambda i: next((n for n, p in enumerate(OPENROUTER_PREFERENCE) if p in i), 99))
            ids = [m.id for m in self.client.models.list()]
            if kind == "groq":
                return [i for i in ids if re.search(r"scout|maverick|vision|vl", i)]
            if kind == "openai":
                return sorted([i for i in ids if re.fullmatch(r"gpt-[\d.]+-mini", i)], reverse=True)
            if kind == "local":
                vision = [i for i in ids if VISION_MODEL.search(i)]
                if not vision:
                    raise ProviderError("Local AI: load a model that can see images (for example Qwen2.5-VL).")
                return vision
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"{self.label}: could not list models ({e}). Is it still running?")
        raise ProviderError(f"{self.label}: set a model name in Settings.")

    def _image_part(self, image: bytes) -> dict:
        return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(image)}"}}

    def start(self, text, image):
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}, self._image_part(image)]})

    def add_results(self, results, image, notes):
        for call, result in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        # Tool messages cannot carry images – the new screenshot goes in a user message.
        text = "\n".join(notes) or "Current screen:"
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}, self._image_part(image)]})

    def _prune(self, keep: int = KEEP_IMAGES):
        seen = 0
        for msg in reversed(self.messages):
            if isinstance(msg.get("content"), list):
                for i, part in enumerate(msg["content"]):
                    if part.get("type") == "image_url":
                        seen += 1
                        if seen > keep:
                            msg["content"][i] = {"type": "text", "text": "[older screenshot]"}

    def forget_images(self) -> bool:
        self._prune(keep=0)
        return True

    def step(self) -> Reply:
        self._prune()
        o = self.openai
        while self.models:
            model = self.models[0]
            try:
                t0 = time.time()
                r = self.client.chat.completions.create(model=model, messages=self.messages, tools=self.tool_defs)
                log.info(f"{model} answered in {time.time() - t0:.1f} s")
                if self.model != model:
                    self.model = model
                    self.emit("status", f"Using {self.label} ({model}).")
                break
            except o.AuthenticationError:
                raise ProviderError(f"{self.label}: the API key is not valid.")
            except (o.RateLimitError, o.APITimeoutError, o.APIConnectionError, o.APIStatusError) as e:
                log.warning(f"{model}: {e}")
                self.models.pop(0)
                if self.models and not self.stop.is_set():
                    self.emit("status", f"{model} is unavailable, trying {self.models[0]}…")
        else:
            raise ProviderError(f"{self.label}: no model answered (limits or server problems).")

        msg = r.choices[0].message
        calls = []
        for c in msg.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(c.id, c.function.name, args))
        entry = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            entry["tool_calls"] = [c.model_dump() for c in msg.tool_calls]
        self.messages.append(entry)
        return Reply(text=(msg.content or "").strip(), calls=calls)


# ---------------------------------------------------------------- Claude


class AnthropicBackend(Backend):
    """Claude through the official Anthropic SDK."""

    DEFAULT_MODEL = "claude-opus-5-5"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=self.entry["key"], timeout=120.0, max_retries=2)
        self.model = self.entry.get("model") or self.DEFAULT_MODEL
        self.messages = []
        self.tool_defs = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                          for t in self.tools]
        self.announced = False

    def _image_block(self, image: bytes) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(image)}}

    def start(self, text, image):
        self.messages.append({"role": "user", "content": [self._image_block(image), {"type": "text", "text": text}]})

    def add_results(self, results, image, notes):
        content = [{"type": "tool_result", "tool_use_id": c.id, "content": r} for c, r in results]
        content += [{"type": "text", "text": n} for n in notes]
        content.append(self._image_block(image))
        self.messages.append({"role": "user", "content": content})

    def step(self) -> Reply:
        a = self.anthropic
        try:
            t0 = time.time()
            # History is only appended to (never edited), so caching and thinking stay valid.
            response = self.client.messages.create(
                model=self.model, max_tokens=16000, system=self.system_prompt, tools=self.tool_defs,
                messages=self.messages, output_config={"effort": "low"}, cache_control={"type": "ephemeral"},
            )
            log.info(f"{self.model} answered in {time.time() - t0:.1f} s")
        except a.AuthenticationError:
            raise ProviderError("Claude: the API key is not valid.")
        except a.NotFoundError:
            raise ProviderError(f"Claude: model {self.model} not found.")
        except (a.RateLimitError, a.APIStatusError, a.APIConnectionError) as e:
            raise ProviderError(f"Claude: {e}")
        if not self.announced:
            self.announced = True
            self.emit("status", f"Using {self.label} ({self.model}).")
        if response.stop_reason == "refusal":
            raise ProviderError("Claude declined this task.")
        self.messages.append({"role": "assistant", "content": response.content})
        return Reply(
            text="".join(b.text for b in response.content if b.type == "text").strip(),
            calls=[ToolCall(b.id, b.name, dict(b.input)) for b in response.content if b.type == "tool_use"],
        )


def make_backend(entry: dict, tools, system_prompt, emit, stop_event, fast: bool) -> Backend:
    kind = entry["type"]
    if kind == "gemini":
        return GeminiBackend(entry, tools, system_prompt, emit, stop_event, fast=fast)
    if kind == "anthropic":
        return AnthropicBackend(entry, tools, system_prompt, emit, stop_event)
    return OpenAIBackend(entry, tools, system_prompt, emit, stop_event)
