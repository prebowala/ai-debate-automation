"""Thumbnail generator for Talked Round.

YouTube shows a thumbnail at about 210 pixels wide in the feed, so anything
that only works at full size does not work at all. Everything here is built
around that: two or three words carrying the image, a hard split in the
channel's own colours, and the supporting line small enough to be a reward
for looking rather than something a viewer has to read.

Two layouts, both from the same episode config:

  split  - the two sources, each with the line it actually says
  clash  - the two lines alone, with the one word that differs picked out

  python thumbnail.py                    every episode in EPISODES
  python thumbnail.py genesis-lie        just that one
  python thumbnail.py who-moved-david \\
      --tag "2 SAMUEL 24" \\
      --left  GOD   "THE LORD MOVED DAVID" \\
      --right SATAN "SATAN PROVOKED DAVID"

The last form needs no edit to this file. Add an episode to EPISODES only
when you want to keep it around and re-render it later.
"""

import argparse
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


def differing_word(a, b):
    """The one word that separates two otherwise identical lines.

    The Genesis pair is "you shall surely die" against "you shall not surely
    die". Lighting up that single word is the whole thumbnail, so it is worth
    finding rather than typing, and it is only correct when exactly one word
    differs.
    """
    def norm(t):
        return [w.strip('",.?!;:').upper() for w in t.split()]

    wa, wb = norm(a), norm(b)
    extra = [w for w in wb if w not in wa]
    missing = [w for w in wa if w not in wb]
    if len(extra) == 1 and not missing:
        return extra[0]
    if len(missing) == 1 and not extra:
        return missing[0]
    return None


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


def fit_wrapped(draw, text, max_w, max_lines, start, floor=40, bold=True):
    """Largest size where text fits max_w across at most max_lines lines.

    A side label is sometimes a whole clause. Shrinking such a label to fit on
    one line makes it unreadable in a feed, and clipping it loses the word that
    says what the side claims, so it is allowed to wrap instead.
    """
    size = start
    while size > floor:
        f = font(size, bold)
        rows = wrap(draw, text, f, max_w)
        if len(rows) <= max_lines and all(
                draw.textlength(r, font=f) <= max_w for r in rows):
            return f, rows
        size -= 4
    f = font(floor, bold)
    return f, wrap(draw, text, f, max_w)


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
    f = fit(d, text, 520, 30, floor=18)
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

    half = 540
    # The badge sits between the two labels, so it only goes in when both of
    # them are single line and there is a gap for it to sit in.
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    if all(len(fit_wrapped(probe, ep[f"{s_}_big"], half - 80, 2, 150, floor=52)[1]) == 1
           for s_ in ("left", "right")):
        draw_vs(d)
    for side, x_centre, colour in (("left", 320, TEAL), ("right", 962, MAGENTA)):
        big = ep[f"{side}_big"]
        line = ep[f"{side}_line"]
        # Narrower than the column so the word clears the badge and the edge.
        fb, big_rows = fit_wrapped(d, big, half - 80, 2, 150, floor=52)

        fl = font(36)
        rows = wrap(d, line, fl, half - 40)
        while len(rows) > 3 and fl.size > 26:
            fl = font(fl.size - 2)
            rows = wrap(d, line, fl, half - 40)

        # Centre the whole block in the space between tag and footer, so the
        # two sides sit level however many lines each of them needs.
        big_h = len(big_rows) * (fb.size + 8)
        block = big_h + 40 + len(rows) * (fl.size + 12)
        y = (130 + 630) // 2 - block // 2
        for row in big_rows:
            shadowed(d, (x_centre, y + fb.size // 2), row, fb, colour)
            y += fb.size + 8
        y += 40
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
    if hl is None:
        hl = differing_word(ep["left_line"], ep["right_line"])
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


def render(ep):
    """Both layouts for one episode. Returns the paths written."""
    os.makedirs(OUT_DIR, exist_ok=True)
    return [
        render_split(ep, os.path.join(OUT_DIR, f"{ep['slug']}-split.png")),
        render_clash(ep, os.path.join(OUT_DIR, f"{ep['slug']}-clash.png")),
    ]


def main():
    ap = argparse.ArgumentParser(
        description="Render Talked Round thumbnails, 1280x720, two layouts each.",
        epilog="With no arguments, renders every episode listed in EPISODES.")
    ap.add_argument("slug", nargs="?",
                    help="render only this episode from EPISODES")
    ap.add_argument("--tag", help="the gold badge, e.g. \"GENESIS 3\"")
    ap.add_argument("--left", nargs=2, metavar=("BIG", "LINE"),
                    help="teal side: one or two words, then the line it says")
    ap.add_argument("--right", nargs=2, metavar=("BIG", "LINE"),
                    help="magenta side: one or two words, then the line it says")
    ap.add_argument("--highlight", metavar="WORD",
                    help="word to pick out in gold; found automatically when "
                         "the two lines differ by exactly one")
    args = ap.parse_args()

    if args.left or args.right:
        missing = [n for n, v in (("--tag", args.tag), ("--left", args.left),
                                  ("--right", args.right), ("slug", args.slug))
                   if not v]
        if missing:
            ap.error("a one-off needs all of: slug, " + ", ".join(
                m for m in missing if m != "slug") or "")
        episodes = [{
            "slug": args.slug,
            "tag": args.tag,
            "left_big": args.left[0], "left_line": args.left[1],
            "right_big": args.right[0], "right_line": args.right[1],
            "clash_highlight": args.highlight,
        }]
    elif args.slug:
        episodes = [e for e in EPISODES if e["slug"] == args.slug]
        if not episodes:
            ap.error(f"no episode named {args.slug!r}. Known: "
                     + ", ".join(e["slug"] for e in EPISODES))
    else:
        episodes = EPISODES

    made = []
    for ep in episodes:
        made += render(ep)
    for path in made:
        print(f"  {path}  ({os.path.getsize(path) // 1024} KB)")
    print(f"\n{len(made)} thumbnails at {W}x{H}. YouTube's limit is 2 MB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
