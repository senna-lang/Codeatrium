#!/usr/bin/env bash
# Regenerate the README terminal-demo SVGs from a scripted `loci` session
# using Freeze. Requires: brew install charmbracelet/tap/freeze
#
# Freeze embeds font glyphs as <path> elements, so the rendering is
# identical across GitHub, local browsers, and other SVG consumers.
#
# These are terminal windows, so the dark window background is intentional
# (unlike the transparent banner). The #161b22 window sits on a 1px border
# (no drop shadow — Freeze's shadow is a fixed opaque black that reads
# heavy on GitHub's light canvas). The border is #484f58 rather than the
# darker #30363d so the window edge stays defined against GitHub's dark
# canvas (#0d1117), where a darker border would nearly vanish.

set -euo pipefail

cd "$(dirname "$0")/.."

render() {
    local which="$1" out="$2"
    FORCE_COLOR=1 uv run python scripts/_demo_render.py "$which" | freeze \
        --output "$out" \
        --window \
        --padding 24,28,24,28 \
        --margin 0 \
        --background '#161b22' \
        --border.radius 10 \
        --border.width 1 \
        --border.color '#484f58' \
        --font.family 'JetBrains Mono' \
        --font.size 14 \
        --line-height 1.35

    # Add a viewBox so the window scales cleanly to the README's requested
    # width instead of being pinned to its intrinsic pixel size.
    DEMO_OUT="$out" python3 - <<'PY'
import os
import re
import pathlib

p = pathlib.Path(os.environ["DEMO_OUT"])
svg = re.sub(
    r'<svg width="([\d.]+)" height="([\d.]+)"',
    r'<svg width="\1" height="\2" viewBox="0 0 \1 \2"',
    p.read_text(),
    count=1,
)
p.write_text(svg)
PY
    echo "wrote: $out"
}

render search assets/demo-search.svg
render context assets/demo-context.svg
