"""Exploded parts diagrams: one shape list per kind of heater, drawn as SVG on the page and as vector art in the
parts-list PDF. Callouts carry their number; the parts list maps each number to a Webb item (checked by staff in the
real library; generated from the catalog here)."""
from __future__ import annotations

from html import escape

W, H = 480, 420
TANK = [("rect", 170, 40, 140, 280), ("ellipse", 240, 40, 70, 9), ("line", 205, 18, 205, 40), ("line", 275, 18, 275, 40),
        ("rect", 196, 10, 18, 9), ("rect", 266, 10, 18, 9)]
SHAPES = {
    "gas_tank": TANK + [("rect", 228, 0, 24, 10), ("rect", 118, 228, 52, 42), ("line", 170, 249, 152, 249),
                        ("dash", 214, 45, 214, 255), ("dash", 266, 45, 266, 285), ("rect", 145, 292, 25, 12),
                        ("rect", 310, 72, 44, 14), ("line", 354, 79, 372, 79), ("rect", 198, 330, 84, 10),
                        ("circle", 252, 325, 5), ("line", 144, 270, 144, 300), ("line", 144, 300, 247, 325)],
    "elec_tank": TANK + [("rect", 300, 118, 30, 26), ("rect", 300, 262, 30, 26), ("line", 230, 131, 300, 131),
                         ("line", 230, 136, 300, 136), ("line", 230, 275, 300, 275), ("line", 230, 280, 300, 280),
                         ("dash", 214, 45, 214, 250), ("dash", 266, 45, 266, 290), ("rect", 145, 292, 25, 12),
                         ("rect", 310, 60, 44, 14), ("line", 354, 67, 372, 67)],
    "tankless": [("rect", 150, 30, 180, 320), ("rect", 175, 60, 130, 70), ("line", 175, 80, 305, 80),
                 ("line", 175, 100, 305, 100), ("circle", 240, 175, 22), ("rect", 185, 225, 110, 18),
                 ("rect", 185, 280, 60, 40), ("rect", 262, 284, 40, 26), ("circle", 304, 236, 5), ("circle", 178, 240, 5),
                 ("rect", 300, 66, 12, 10), ("line", 200, 350, 200, 380), ("rect", 192, 356, 16, 12),
                 ("line", 282, 350, 282, 380)],
}
# callout number -> (anchor point on the part, label position)
CALLOUTS = {
    "gas_tank": {1: ((144, 249), (70, 210)), 2: ((200, 312), (110, 382)), 3: ((252, 325), (330, 380)),
                 4: ((240, 340), (240, 400)), 5: ((214, 150), (110, 120)), 6: ((266, 180), (390, 175)),
                 7: ((157, 298), (80, 322)), 8: ((332, 79), (410, 50))},
    "elec_tank": {1: ((315, 131), (410, 120)), 2: ((315, 275), (410, 262)), 3: ((262, 133), (390, 190)),
                  4: ((262, 277), (380, 330)), 5: ((214, 150), (110, 120)), 6: ((266, 200), (130, 210)),
                  7: ((157, 298), (80, 322)), 8: ((332, 67), (410, 40))},
    "tankless": {1: ((215, 300), (90, 330)), 2: ((304, 236), (400, 250)), 3: ((178, 240), (90, 240)),
                 4: ((282, 297), (400, 310)), 5: ((240, 175), (400, 170)), 6: ((200, 362), (110, 395)),
                 7: ((306, 71), (400, 50)), 8: ((240, 95), (90, 90))},
}


def svg(cls: str, rows: list[dict], hot: int | None = None) -> str:
    out = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" class="kcdiag" role="img" '
           f'aria-label="Exploded parts diagram">', '<g fill="none" stroke="#6b6e74" stroke-width="2">']
    for s in SHAPES[cls]:
        k = s[0]
        if k == "rect":
            out.append(f'<rect x="{s[1]}" y="{s[2]}" width="{s[3]}" height="{s[4]}" rx="5" fill="#fff"/>')
        elif k == "ellipse":
            out.append(f'<ellipse cx="{s[1]}" cy="{s[2]}" rx="{s[3]}" ry="{s[4]}"/>')
        elif k == "circle":
            out.append(f'<circle cx="{s[1]}" cy="{s[2]}" r="{s[3]}" fill="#fff"/>')
        elif k in ("line", "dash"):
            out.append(f'<line x1="{s[1]}" y1="{s[2]}" x2="{s[3]}" y2="{s[4]}"'
                       + (' stroke-dasharray="5 4"' if k == "dash" else "") + "/>")
    out.append("</g>")
    have = {r["n"]: r for r in rows}
    for n, ((ax, ay), (lx, ly)) in CALLOUTS[cls].items():
        if n not in have:
            continue
        r = have[n]
        c = "#e0353b" if n == hot else "#1c1e22"
        out.append(f'<g class="co" data-n="{n}" data-sku="{escape(r["sku"])}" tabindex="0" role="button" '
                   f'aria-label="{n}: {escape(r["part"])}" style="cursor:pointer">'
                   f'<line x1="{ax}" y1="{ay}" x2="{lx}" y2="{ly}" stroke="{c}" stroke-width="1.5"/>'
                   f'<circle cx="{ax}" cy="{ay}" r="3" fill="{c}"/><circle cx="{lx}" cy="{ly}" r="13" fill="{c}"/>'
                   f'<text x="{lx}" y="{ly + 4}" text-anchor="middle" fill="#fff" '
                   f'style="font:700 12px Inter,Helvetica,Arial,sans-serif">{n}</text></g>')
    out.append("</svg>")
    return "".join(out)


def callouts(cls: str, rows: list[dict]) -> list[dict]:
    pos = CALLOUTS[cls]
    return [{**r, "x": pos[r["n"]][1][0], "y": pos[r["n"]][1][1]} for r in rows if r["n"] in pos]
