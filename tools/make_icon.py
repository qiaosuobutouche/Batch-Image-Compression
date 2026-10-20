"""生成应用图标 assets/app.ico。"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

SIZE = 1024
ROOT = Path(__file__).resolve().parents[1]


def draw_icon() -> Image.Image:
    gradient = ImageOps.colorize(Image.linear_gradient("L").resize((SIZE, SIZE)), "#5b8dff", "#2a52c9")
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, SIZE - 40, SIZE - 40), radius=220, fill=255)
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(gradient, mask=mask)

    draw = ImageDraw.Draw(icon)
    white = (255, 255, 255, 255)
    frame = (190, 250, 834, 774)
    draw.rounded_rectangle(frame, radius=70, outline=white, width=56)
    draw.ellipse((330, 360, 450, 480), fill=white)
    draw.polygon([(250, 714), (450, 500), (580, 640), (650, 570), (774, 714)], fill=white)

    badge = (640, 600, 960, 920)
    draw.ellipse(badge, fill=(255, 138, 61, 255), outline=(42, 82, 201, 255), width=28)
    cx = (badge[0] + badge[2]) // 2
    draw.rectangle((cx - 34, 668, cx + 34, 790), fill=white)
    draw.polygon([(cx - 92, 780), (cx + 92, 780), (cx, 870)], fill=white)
    return icon


def main() -> None:
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    icon = draw_icon()
    icon.resize((256, 256), Image.Resampling.LANCZOS).save(assets / "app.png")
    icon.save(assets / "app.ico", format="ICO",
              sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon written:", assets / "app.ico")


if __name__ == "__main__":
    main()
