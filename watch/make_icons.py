"""Regenerates the Wristle icons in res/ (needs Pillow). Usage: python make_icons.py res preview.png"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

RES = Path(sys.argv[1])
PREVIEW = Path(sys.argv[2])
SS = 8  # supersampling

TEAL = ((45, 212, 191), (13, 148, 136))   # top -> bottom
RED = ((251, 113, 133), (225, 29, 72))


def gradient_circle(n, top, bottom):
    grad = Image.new("RGBA", (1, n))
    for y in range(n):
        t = y / (n - 1)
        grad.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    grad = grad.resize((n, n))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, n - 1, n - 1), fill=255)
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def mic(draw, cx, cy, s, color, waves=False):
    """Mic glyph centred at (cx, cy); s = glyph height. Stroke width scales with s."""
    w = s * 0.095                        # stroke
    cap_w, cap_h = s * 0.34, s * 0.52
    top = cy - s * 0.5
    draw.rounded_rectangle((cx - cap_w / 2, top, cx + cap_w / 2, top + cap_h),
                           radius=cap_w / 2, fill=color)
    # U-shaped holder
    r = s * 0.29
    acy = top + cap_h - cap_w / 2
    draw.arc((cx - r, acy - r, cx + r, acy + r), 0, 180, fill=color, width=int(w))
    for x in (cx - r + w / 2, cx + r - w / 2):  # round caps on the U's ends
        draw.ellipse((x - w / 2, acy - w / 2, x + w / 2, acy + w / 2), fill=color)
    # stem + base
    stem_top, stem_bot = acy + r, cy + s * 0.5 - w / 2
    draw.line((cx, stem_top, cx, stem_bot), fill=color, width=int(w))
    draw.rounded_rectangle((cx - s * 0.17, stem_bot - w / 2, cx + s * 0.17, stem_bot + w / 2),
                           radius=w / 2, fill=color)
    if waves:  # sound waves on both sides of the capsule
        wcy = top + cap_h * 0.38
        ww = w * 0.85
        for rr, span in ((s * 0.46, 30), (s * 0.64, 26)):
            for start in (-span, 180 - span):
                box = (cx - rr, wcy - rr, cx + rr, wcy + rr)
                draw.arc(box, start, start + 2 * span, fill=color, width=int(ww))
                for ang in (start, start + 2 * span):  # round caps
                    import math
                    a = math.radians(ang)
                    px, py = cx + (rr - ww / 2) * math.cos(a), wcy + (rr - ww / 2) * math.sin(a)
                    draw.ellipse((px - ww / 2, py - ww / 2, px + ww / 2, py + ww / 2), fill=color)


def badge(n, colors, waves, glyph=0.5):
    big = n * SS
    img = gradient_circle(big, *colors)
    d = ImageDraw.Draw(img)
    mic(d, big / 2, big / 2, big * glyph, (255, 255, 255), waves)
    return img.resize((n, n), Image.LANCZOS)


def mono(n):
    big = n * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    mic(ImageDraw.Draw(img), big / 2, big / 2, big * 0.74, (255, 255, 255), waves=False)
    return img.resize((n, n), Image.LANCZOS)


DENS = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}
for name, f in DENS.items():
    for folder in (f"mipmap-{name}", f"drawable-{name}"):
        (RES / folder).mkdir(exist_ok=True)
    badge(int(48 * f), TEAL, waves=True, glyph=0.42).save(RES / f"mipmap-{name}/ic_launcher.png", optimize=True)
    badge(int(48 * f), RED, waves=False, glyph=0.5).save(RES / f"mipmap-{name}/ic_dictate.png", optimize=True)
    mono(int(24 * f)).save(RES / f"drawable-{name}/ic_complication.png", optimize=True)

# Big glyph for the in-app mic button (drawn at ~40% of the screen width, so no density buckets).
(RES / "drawable-nodpi").mkdir(exist_ok=True)
mono(256).save(RES / "drawable-nodpi/ic_mic.png", optimize=True)

# Preview: icons + a mock watch face with the complication as colour image and as tinted mono icon.
W = 1040
pv = Image.new("RGBA", (W, 420), (24, 24, 27, 255))
d = ImageDraw.Draw(pv)
x = 30
for label, im in (("launcher", Image.open(RES / "mipmap-xxxhdpi/ic_launcher.png")),
                  ("Wristle Dictate", Image.open(RES / "mipmap-xxxhdpi/ic_dictate.png")),
                  ("mono", Image.open(RES / "drawable-xxxhdpi/ic_complication.png").resize((192, 192)))):
    if label == "mono":
        d.rounded_rectangle((x, 40, x + 192, 232), 24, fill=(60, 60, 66, 255))
    pv.alpha_composite(im, (x, 40))
    d.text((x, 245), label, fill=(200, 200, 200, 255))
    x += 220
face = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
fd = ImageDraw.Draw(face)
fd.ellipse((0, 0, 299, 299), fill=(0, 0, 0, 255), outline=(70, 70, 70, 255), width=3)
fd.text((118, 120), "10:09", fill=(255, 255, 255, 255))
slot = Image.open(RES / "mipmap-xxxhdpi/ic_dictate.png").resize((64, 64), Image.LANCZOS)
face.alpha_composite(slot, (118, 190))
m = Image.open(RES / "drawable-xxxhdpi/ic_complication.png").resize((34, 34), Image.LANCZOS)
tint = Image.new("RGBA", m.size, (125, 211, 252, 255))
tint.putalpha(m.getchannel("A"))
fd.ellipse((40, 118, 100, 178), fill=(30, 41, 59, 255))
face.alpha_composite(tint, (53, 131))
pv.alpha_composite(face, (W - 320, 60))
d.text((W - 320, 370), "watch face: colour image (bottom), tinted mono (left)", fill=(200, 200, 200, 255))
pv.save(PREVIEW)
print("ok")
