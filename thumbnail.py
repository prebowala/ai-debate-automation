"""Thumbnail generator for Talked Round.

YouTube shows a thumbnail at about 210 pixels wide in the feed, so anything
that only works at full size does not work at all. Everything here is built
around that: two or three words carrying the image, a hard split in the
channel's own colours, and the supporting line small enough to be a reward
for looking rather than something a viewer has to read.

Two layouts, both from the same episode config:

  split  - the two sources, each with the line it actually says
  clash  - the two lines alone, with the one word that differs picked out

Run it with no arguments to render every episode in EPISODES to thumbs/.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
OUT_DIR = "thumbs"

BG = (10, 14, 24)
LEFT_WASH = (7, 34, 31)
RIGHT_WASH = (35, 8, 30)
TEAL = (0, 255, 204)
MAGENTA = (255, 92, 224)
GOLD = (255, 216, 74)
WHITE = (245, 247, 245)
DIM = (168, 178, 190)

BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


# --------------------------------------------------------------------------
# Episodes. Keep BIG to one or two short words: it is the only thing that
# survives being shrunk to a thumbnail in a phone feed.
# --------------------------------------------------------------------------
EPISODES = [
    {
        "slug": "genesis-lie",
        "tag": "GENESIS 3",
        "left_big": "GOD",
        "left_line": "YOU SHALL SURELY DIE",
        "right_big": "SERPENT",
        "right_line": "YOU SHALL NOT SURELY DIE",
        "clash_highlight": "NOT",
    },
    {
        "slug": "jesus-claim",
        "tag": "WHO DID HE SAY HE WAS",
        "left_big": "JOHN",
        "left_line": "BEFORE ABRAHAM WAS, I AM",
        "right_big": "MARK",
        "right_line": "WHY CALLEST THOU ME GOOD?",
        "clash_highlight": None,
    },
]


def font(size, bold=True):
    return ImageFont.truetype(BOLD if bold else REG, size)


def fit(draw, text, max_w, start, floor=28, bold=True):
    """Largest size at or below start that keeps text inside max_w."""
    size = start
    while size > floor:
        f = font(size, bold)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return font(floor, bold)


def wrap(draw, text, f, max_w):
    lines, line = [], ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if draw.textlength(trial, font=f) <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def shadowed(draw, xy, text, f, fill, anchor="mm", blur=4):
    """Cheap drop shadow. Thumbnails sit on unpredictable feed backgrounds."""
    x, y = xy
    for dx, dy in ((blur, blur), (blur, -blur), (-blur, blur), (-blur, -blur)):
        draw.text((x + dx, y + dy), text, font=f, fill=(0, 0, 0), anchor=anchor)
    draw.text((x, y), text, font=f, fill=fill, anchor=anchor)


def base_image(ep):
    """Dark ground, split diagonally, one side per debater colour."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    top_x, bot_x = 672, 608          # a slight lean, so it is not a plain column
    d.polygon([(0, 0), (top_x, 0), (bot_x, H), (0, H)], fill=LEFT_WASH)
    d.polygon([(top_x, 0), (W, 0), (W, H), (bot_x, H)], fill=RIGHT_WASH)

    # Colour bleeding in from each outer edge, so the sides read as sides.
    # Composited with falling alpha rather than painted on: drawing a flat
    # colour over the wash left a visible seam where the run ended.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    span = 320
    for i in range(span):
        a = int(46 * (1 - i / span) ** 2)
        gd.line([(i, 0), (i, H)], fill=TEAL + (a,))
        gd.line([(W - 1 - i, 0), (W - 1 - i, H)], fill=MAGENTA + (a,))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(img)
    d.line([(top_x, 0), (bot_x, H)], fill=GOLD, width=5)
    return img, d


def draw_tag(d, text):
    f = font(30)
    tw = d.textlength(text, font=f)
    d.rounded_rectangle([44, 36, 44 + tw + 44, 36 + 56], radius=8, fill=GOLD)
    d.text((44 + 22, 36 + 28), text, font=f, fill=(12, 16, 26), anchor="lm")


def draw_footer(d):
    d.rectangle([0, H - 74, W, H], fill=(6, 9, 16))
    d.line([(0, H - 74), (W, H - 74)], fill=(40, 48, 62), width=2)
    d.text((44, H - 37), "TALKED ROUND", font=font(30), fill=GOLD, anchor="lm")
    d.text((W - 44, H - 37), "AN AI JURY DECIDES", font=font(30, bold=False),
           fill=DIM, anchor="rm")


def draw_vs(d):
    cx, cy, r = W // 2, 330, 58
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(10, 14, 24), outline=GOLD, width=5)
    d.text((cx, cy + 2), "VS", font=font(46), fill=GOLD, anchor="mm")


def render_split(ep, path):
    """Big source name, and underneath it the line that source actually gives."""
    img, d = base_image(ep)
    draw_tag(d, ep["tag"])
    draw_vs(d)

    half = 540
    for side, x_centre, colour in (("left", 320, TEAL), ("right", 962, MAGENTA)):
        big = ep[f"{side}_big"]
        line = ep[f"{side}_line"]
        # Narrower than the column so the word clears the badge and the edge.
        fb = fit(d, big, half - 80, 150, floor=60)

        fl = font(36)
        rows = wrap(d, line, fl, half - 40)
        while len(rows) > 3 and fl.size > 26:
            fl = font(fl.size - 2)
            rows = wrap(d, line, fl, half - 40)

        # Centre the whole block in the space between tag and footer, so the
        # two sides sit level however many lines each of them needs.
        block = fb.size + 46 + len(rows) * (fl.size + 12)
        y = (130 + 630) // 2 - block // 2
        shadowed(d, (x_centre, y + fb.size // 2), big, fb, colour)
        y += fb.size + 46
        for row in rows:
            shadowed(d, (x_centre, y + fl.size // 2), row, fl, WHITE, blur=3)
            y += fl.size + 12

    draw_footer(d)
    img.save(path, quality=92)
    return path


def render_clash(ep, path):
    """The two lines alone. Where one word differs, that word is picked out."""
    img, d = base_image(ep)
    draw_tag(d, ep["tag"])

    half = 560
    hl = ep.get("clash_highlight")
    for side, x0, colour in (("left", 40, TEAL), ("right", W - 600, MAGENTA)):
        line = ep[f"{side}_line"]
        f = font(58)
        rows = wrap(d, line, f, half)
        while len(rows) > 4 and f.size > 34:
            f = font(f.size - 4)
            rows = wrap(d, line, f, half)

        y = (130 + 630) // 2 - (len(rows) * (f.size + 14)) // 2
        for row in rows:
            # Lay the row out word by word so one word can be lit up.
            words = row.split()
            widths = [d.textlength(w + " ", font=f) for w in words]
            x = x0 + (half - (sum(widths) - d.textlength(" ", font=f))) / 2
            for w, wd in zip(words, widths):
                lit = hl and w.strip('",.?!').upper() == hl.upper()
                shadowed(d, (x, y), w, f, GOLD if lit else colour, anchor="lt", blur=3)
                x += wd
            y += f.size + 14

    draw_footer(d)
    img.save(path, quality=92)
    return path


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    made = []
    for ep in EPISODES:
        made.append(render_split(ep, os.path.join(OUT_DIR, f"{ep['slug']}-split.png")))
        made.append(render_clash(ep, os.path.join(OUT_DIR, f"{ep['slug']}-clash.png")))
    for p in made:
        kb = os.path.getsize(p) // 1024
        print(f"  {p}  ({kb} KB)")
    print(f"\n{len(made)} thumbnails at {W}x{H}. YouTube's limit is 2 MB.")


if __name__ == "__main__":
    sys.exit(main())
