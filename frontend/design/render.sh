#!/usr/bin/env bash
# Regenerates the raster brand assets in ../public from their SVG sources.
#
# Run this after editing design/og.svg or public/icon.svg. The rasters are
# committed rather than generated at build time: they are needed by link-preview
# crawlers and by browsers that will not take an SVG, and neither of those can
# wait for a build step.
#
# Headless Chrome does the rasterising, not ImageMagick. ImageMagick's built-in
# SVG renderer silently drops linearGradient fills and mangles tspan/letter-
# spacing, which turned the first attempt at og.png into grey overlapping text.
# ImageMagick is still used for the .ico, where the input is already a PNG.
#
# Chrome renders a standalone .svg at its *intrinsic* width/height and ignores
# the window size, so each icon size is rendered from a copy with width/height
# rewritten to match. Do not "simplify" that away.
set -euo pipefail

cd "$(dirname "$0")"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
SHOT=(--headless --disable-gpu --hide-scrollbars --default-background-color=00000000)

"$CHROME" "${SHOT[@]}" --window-size=1200,630 \
  --screenshot=../public/og.png "file://$PWD/og.svg"

# Two different sources, for one reason: iOS ignores transparency on a home
# screen icon and composites it onto its own opaque background, so the
# plate-less mark would land as a gradient "e" on black. apple-icon.svg keeps
# a plate; everything else uses the transparent public/icon.svg.
render_icon() {  # render_icon <source-svg> <size> <output-png>
  sed "s|width=\"64\" height=\"64\"|width=\"$2\" height=\"$2\"|" "$1" > "/tmp/ea-src-$2.svg"
  "$CHROME" "${SHOT[@]}" --window-size="$2,$2" \
    --screenshot="$3" "file:///tmp/ea-src-$2.svg"
}

render_icon ../public/icon.svg 256 /tmp/ea-icon-256.png
render_icon apple-icon.svg 180 ../public/apple-touch-icon.png

magick /tmp/ea-icon-256.png -define icon:auto-resize=64,48,32,16 ../public/favicon.ico

echo "rendered: og.png apple-touch-icon.png favicon.ico"
