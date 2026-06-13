#!/usr/bin/env bash
# Regenerate assets/banner.svg from the live loci banner using Freeze.
# Requires: brew install charmbracelet/tap/freeze
#
# Freeze embeds font glyphs as <path> elements so the rendering is
# identical across GitHub, local browsers, and other SVG consumers.
#
# Post-processing:
# - sed: Freeze drops the alpha channel of `--background '#00000000'` and
#   emits an opaque black rect, so we rewrite it to fill="none" for a
#   transparent background that adapts to GitHub light/dark mode (starship
#   style). Rich's `dim` style renders as #c4c4c4, unreadable on white, so
#   remap it to a mid-gray legible on both modes.
# - python: stretch the three CODEATRIUM half-block lines vertically by
#   BANNER_VSCALE. The wordmark is plain <text> in JetBrains Mono, so a
#   non-uniform scale on just those rows makes the letters taller without
#   touching the box, tagline, or version string. The rows are found by
#   their gradient fills (no hard-coded coordinates) so the step survives
#   font-size / padding changes.

set -euo pipefail

cd "$(dirname "$0")/.."

BANNER_VSCALE="${BANNER_VSCALE:-1.25}"

FORCE_COLOR=1 uv run python -c "
from codeatrium.cli import _print_banner
_print_banner()
" | freeze \
    --output assets/banner.svg \
    --padding 4,6,4,6 \
    --margin 0 \
    --background '#00000000' \
    --border.radius 8 \
    --font.family 'JetBrains Mono' \
    --font.size 14 \
    --line-height 1.2

sed -i '' \
    -e 's/<rect \(width="[^"]*" height="[^"]*"\) fill="#000000"/<rect \1 fill="none"/' \
    -e 's/fill="#c4c4c4"/fill="#8a8a8a"/g' \
    assets/banner.svg

BANNER_VSCALE="$BANNER_VSCALE" python3 - <<'PY'
import os
import re
import pathlib

scale = float(os.environ["BANNER_VSCALE"])
path = pathlib.Path("assets/banner.svg")
svg = path.read_text()

# The CODEATRIUM wordmark is three consecutive <text> rows carrying the
# blue gradient fills; everything else (box, tagline, version) is untouched.
gradient = ("#7bb8ff", "#4a9eff", "#1b45a8")
rows = [
    m for m in re.finditer(r"<text\b[^>]*>.*?</text>", svg)
    if any(c in m.group(0) for c in gradient)
]
if len(rows) != 3:
    raise SystemExit(f"expected 3 gradient rows, found {len(rows)}")

ys = [float(re.search(r'y="([\d.]+)px"', m.group(0)).group(1)) for m in rows]
cy = sum(ys) / len(ys)
start, end = rows[0].start(), rows[-1].end()
group = (
    f'<g transform="translate(0 {cy}) scale(1 {scale}) translate(0 -{cy})">'
    + svg[start:end]
    + "</g>"
)
out = svg[:start] + group + svg[end:]

# Freeze positions each line by its baseline and reserves a full
# line-box (1.2 leading) below the last line, while the top border glyph
# sits high in its first line-box — so equal top/bottom --padding still
# leaves ~10px more empty space under the bottom border than above the
# top. Crop the canvas height so the bottom gap equals the top gap.
baselines = [float(y) for y in re.findall(r'<text x="[^"]*" y="([\d.]+)px"', out)]
top_gap = baselines[0]
new_h = baselines[0] + baselines[-1]  # bottom gap == top gap
width = re.search(r'<svg width="([\d.]+)"', out).group(1)

# Freeze emits a root <svg> with width/height but no viewBox, so scaling
# it (README width="720" on a 506-wide canvas) leaves the artwork pinned
# top-left with empty canvas. Add a viewBox (at the cropped height) so the
# whole banner scales to fit whatever width the README requests.
out = re.sub(
    r'<svg width="[\d.]+" height="[\d.]+"',
    f'<svg width="{width}" height="{new_h:.2f}" viewBox="0 0 {width} {new_h:.2f}"',
    out,
    count=1,
)
# Match the (invisible) background rect height to the cropped canvas.
out = re.sub(
    r'(<rect width="[\d.]+" height=")[\d.]+(" fill="none")',
    rf"\g<1>{new_h:.2f}\g<2>",
    out,
    count=1,
)
path.write_text(out)
PY

echo "wrote: assets/banner.svg (transparent background, wordmark x${BANNER_VSCALE} vertical)"
