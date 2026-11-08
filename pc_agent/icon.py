"""App icon drawn in code (tray icon, window icon and the .exe icon)."""

from PIL import Image, ImageDraw


def make_icon(size: int = 256, paused: bool = False) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    color = (245, 176, 65) if paused else (79, 110, 240)
    d.rounded_rectangle([2 * s, 2 * s, 62 * s, 62 * s], radius=14 * s, fill=color)
    # Monitor with a cursor arrow.
    d.rounded_rectangle([13 * s, 15 * s, 51 * s, 41 * s], radius=4 * s, outline="white", width=round(4 * s))
    d.rectangle([28 * s, 41 * s, 36 * s, 47 * s], fill="white")
    d.rounded_rectangle([20 * s, 46 * s, 44 * s, 50 * s], radius=2 * s, fill="white")
    d.polygon([(27 * s, 21 * s), (27 * s, 36 * s), (31 * s, 32 * s), (35 * s, 38 * s),
               (37 * s, 37 * s), (33 * s, 31 * s), (38 * s, 30 * s)], fill="white")
    return img


if __name__ == "__main__":
    # python -m pc_agent.icon -> writes assets/icon.ico for the .exe build
    from pathlib import Path

    out = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
    out.parent.mkdir(exist_ok=True)
    make_icon(256).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(out)
