"""„Oči“ appky: nepretržité snímanie obrazovky na pozadí a detekcia zmien.

AI nedostáva stream – vypýta si snímku, keď ju potrebuje. Appka ju má vždy
pripravenú a vie povedať, kedy a kde sa obrazovka zmenila.
"""

import ctypes
import io
import threading
import time
from collections import deque

import mss
from PIL import Image, ImageChops, ImageStat

# Prah priemerného rozdielu (0–255) na zmenšenej snímke, nad ktorým berieme obrazovku za zmenenú.
CHANGE_THRESHOLD = 1.5
# Privacy: with nobody watching and no AI working, capturing stops and the last picture is deleted.
IDLE_AFTER_S = 10


class LiveScreen:
    def __init__(self, fps: float = 4, max_width: int = 1280, jpeg_quality: int = 70):
        self.interval = 1.0 / max(fps, 0.5)
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        self._lock = threading.Lock()
        self._frame: Image.Image | None = None
        self._thumb: Image.Image | None = None
        self._new_frame = threading.Condition(self._lock)
        self._wanted_until = 0.0
        self._wake = threading.Event()
        self.monitor: dict = {}
        self.last_change = time.time()
        self.changes: deque[tuple[float, float]] = deque(maxlen=200)
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        self._wait_for_frame()  # the screen size is needed right away

    def want(self, seconds: float = IDLE_AFTER_S):
        """Someone needs the screen (live view, the AI, a tool) – keep capturing for a while."""
        self._wanted_until = max(self._wanted_until, time.time() + seconds)
        self._wake.set()

    @property
    def capturing(self) -> bool:
        return time.time() < self._wanted_until

    def _wait_for_frame(self, timeout: float = 5) -> Image.Image:
        self.want()
        with self._new_frame:
            self._new_frame.wait_for(lambda: self._frame is not None, timeout)
            if self._frame is None:
                raise RuntimeError("Could not capture the screen.")
            return self._frame

    def _forget(self):
        with self._lock:
            self._frame = self._thumb = None

    def _loop(self):
        user32 = ctypes.windll.user32
        while True:
            try:
                with mss.MSS() as sct:
                    monitor = sct.monitors[1]  # primary monitor
                    with self._lock:
                        self.monitor = monitor
                    while True:
                        if not self.capturing:
                            self._forget()
                            self._wake.wait()
                            self._wake.clear()
                        # Games switch the display resolution – start over with the new size.
                        if (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)) != (monitor["width"], monitor["height"]):
                            break
                        self._capture(sct, monitor)
                        time.sleep(self.interval)
            except Exception:
                # The capture fails for a moment while a game changes display mode.
                time.sleep(0.5)

    def _capture(self, sct, monitor):
        raw = sct.grab(monitor)
        frame = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        thumb = frame.resize((160, 90)).convert("L")
        with self._lock:
            if self._thumb is not None:
                diff = ImageStat.Stat(ImageChops.difference(thumb, self._thumb)).mean[0]
                if diff > CHANGE_THRESHOLD:
                    now = time.time()
                    self.last_change = now
                    self.changes.append((now, diff))
            self._frame, self._thumb = frame, thumb
            self._new_frame.notify_all()

    @property
    def is_black(self) -> bool:
        """Nearly black screen – usually a game in exclusive fullscreen (Windows hides it) or loading."""
        with self._lock:
            thumb = self._thumb
        return thumb is not None and ImageStat.Stat(thumb).mean[0] < 6

    @property
    def scale(self) -> float:
        """Koľko skutočných pixelov pripadá na 1 pixel screenshotu pre AI."""
        width = self.monitor.get("width", self.max_width)
        return max(width / self.max_width, 1.0)

    @property
    def shot_size(self) -> tuple[int, int]:
        """Size of the screenshot the AI receives."""
        return (round(self.monitor.get("width", 1) / self.scale), round(self.monitor.get("height", 1) / self.scale))

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """Prevod súradníc zo screenshotu (čo vidí AI) na skutočné súradnice obrazovky."""
        return (
            int(self.monitor.get("left", 0) + x * self.scale),
            int(self.monitor.get("top", 0) + y * self.scale),
        )

    def jpeg(self) -> bytes:
        frame = self._wait_for_frame()
        scale = max(frame.width / self.max_width, 1.0)  # the frame's own size – it may predate a resolution change
        if scale > 1:
            frame = frame.resize((round(frame.width / scale), round(frame.height / scale)))
        buf = io.BytesIO()
        frame.save(buf, format="JPEG", quality=self.jpeg_quality)
        return buf.getvalue()

    def wait_for_change(self, timeout: float, settle: float = 0.4) -> bool:
        """Čaká, kým sa obrazovka zmení a potom ustáli. Vráti False pri timeoute."""
        self.want(timeout + settle + 1)
        self._wait_for_frame()  # a first picture to compare against
        start = time.time()
        deadline = start + timeout
        while time.time() < deadline:
            if self.last_change > start:
                # Zmena nastala – počkáme, kým prestane blikať/načítavať sa.
                while time.time() < deadline and time.time() - self.last_change < settle:
                    time.sleep(0.1)
                return True
            time.sleep(0.1)
        return False

    def activity_summary(self, seconds: float) -> str:
        now = time.time()
        recent = [(t, d) for t, d in self.changes if now - t <= seconds]
        if not recent:
            return f"The screen has not changed in the last {seconds:.0f} s."
        biggest = max(d for _, d in recent)
        return (
            f"Last {seconds:.0f} s: {len(recent)} screen changes, "
            f"latest {now - recent[-1][0]:.1f} s ago, biggest intensity {biggest:.1f}."
        )
