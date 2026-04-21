#!/usr/bin/env bash
# Regenerate assets/banner.svg from the live loci banner using Freeze.
# Requires: brew install charmbracelet/tap/freeze
#
# Freeze embeds font glyphs as <path> elements so the rendering is
# identical across GitHub, local browsers, and other SVG consumers.

set -euo pipefail

cd "$(dirname "$0")/.."

FORCE_COLOR=1 python -c "
from codeatrium.cli import _print_banner
_print_banner()
" | freeze \
    --output assets/banner.svg \
    --padding 24,28,24,28 \
    --margin 0 \
    --background '#00000000' \
    --border.radius 8 \
    --font.family 'JetBrains Mono' \
    --font.size 14 \
    --line-height 1.2

echo "wrote: assets/banner.svg"
