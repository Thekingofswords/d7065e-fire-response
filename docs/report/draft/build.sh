#!/bin/sh
# Build report.pdf from report.md (pandoc + XeLaTeX), from this folder.
set -e
cd "$(dirname "$0")"
TEXINPUTS=".:./tex:${TEXINPUTS:-}" pandoc report.md -o report.pdf --pdf-engine=xelatex -V mainfont="Latin Modern Roman" \
  -V monofont="Latin Modern Mono" --resource-path=. --highlight-style=tango --lua-filter=breakcode.lua
echo "wrote report.pdf"
