#!/usr/bin/env python3
"""Render assets/logo.svg — pixel wordmark 'LOCAL0'.
All glyphs share one 5x7 cap grid (no glyph overshoots), background transparent,
orange gradient distinct from the Hermes gold. Re-run after editing FONT/COLORS.
"""

# 5 wide x 7 tall. '#'=on. Same height for every glyph — the A/0 no longer overshoot.
FONT = {
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "A": ["..#..", ".#.#.", "#...#", "#####", "#...#", "#...#", "#...#"],
    "0": [".###.", "#..##", "#.#.#", "#.#.#", "##..#", "#...#", ".###."],
}

TEXT = "LOCAL0"
CELL = 16          # fill pixel size
PAD = 22           # canvas margin
GAP = 1            # blank columns between glyphs
COLORS = ["#ffc48a", "#f5842f", "#e0561c", "#b8380a"]  # top->bottom, orange (not Hermes gold)


def cells(text):
    """Yield (col, row) on-pixels in absolute grid coords, plus total width in cols."""
    x = 0
    on = []
    for ch in text:
        g = FONT[ch]
        for r, line in enumerate(g):
            for c, px in enumerate(line):
                if px == "#":
                    on.append((x + c, r))
        x += len(g[0]) + GAP
    return on, x - GAP  # drop trailing gap


def render():
    on = set(cells(TEXT)[0])
    cols = cells(TEXT)[1]
    W = PAD * 2 + cols * CELL
    H = PAD * 2 + 7 * CELL

    def px(col, row):
        return PAD + col * CELL, PAD + row * CELL

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
        '  <defs>',
        '    <linearGradient id="g" gradientUnits="userSpaceOnUse" '
        f'x1="0" y1="{PAD}" x2="0" y2="{PAD + 7 * CELL}">',
    ]
    for i, c in enumerate(COLORS):
        out.append(f'      <stop offset="{int(100 * i / (len(COLORS) - 1))}%" stop-color="{c}"/>')
    out += ['    </linearGradient>', '  </defs>']

    # Layer 1: drop shadow (dark outline shape, offset).
    for col, row in sorted(on):
        x, y = px(col, row)
        out.append(f'  <rect x="{x - 4 + 6}" y="{y - 4 + 8}" width="24" height="24" fill="#000" opacity="0.4"/>')
    # Layer 2: dark outline (24px merges neighbours into a connected border).
    for col, row in sorted(on):
        x, y = px(col, row)
        out.append(f'  <rect x="{x - 4}" y="{y - 4}" width="24" height="24" fill="#150c00"/>')
    # Layer 3: gradient fill.
    for col, row in sorted(on):
        x, y = px(col, row)
        out.append(f'  <rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" fill="url(#g)"/>')
    # Layer 4: bevel only on exposed edges (neighbour-aware).
    for col, row in sorted(on):
        x, y = px(col, row)
        if (col, row - 1) not in on:
            out.append(f'  <rect x="{x}" y="{y}" width="{CELL}" height="4" fill="#fff3c4" opacity="0.55"/>')
        if (col - 1, row) not in on:
            out.append(f'  <rect x="{x}" y="{y}" width="3" height="{CELL}" fill="#fff3c4" opacity="0.35"/>')
        if (col, row + 1) not in on:
            out.append(f'  <rect x="{x}" y="{y + CELL - 4}" width="{CELL}" height="4" fill="#5a2600" opacity="0.55"/>')
        if (col + 1, row) not in on:
            out.append(f'  <rect x="{x + CELL - 3}" y="{y}" width="3" height="{CELL}" fill="#5a2600" opacity="0.45"/>')

    out.append('</svg>')
    return "\n".join(out) + "\n"


def _check():
    for ch, g in FONT.items():
        assert len(g) == 7, f"{ch}: not 7 rows"
        assert all(len(r) == 5 for r in g), f"{ch}: not 5 cols"
    assert all(c in FONT for c in TEXT), "TEXT has glyph missing from FONT"


if __name__ == "__main__":
    _check()
    import pathlib
    p = pathlib.Path(__file__).parent / "assets" / "logo.svg"
    p.write_text(render())
    print(f"wrote {p}")
