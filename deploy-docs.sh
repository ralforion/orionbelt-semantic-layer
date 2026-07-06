#!/bin/bash
# Deploy OrionBelt docs to ralforion.github.io

set -e

echo "📚 Building OrionBelt documentation..."
uv run mkdocs build -d ../ralforion.github.io/public/orionbelt-semantic-layer

echo "📦 Committing to ralforion.github.io..."
cd ../ralforion.github.io
git add public/orionbelt-semantic-layer
git commit -m "Update OrionBelt docs from $(cd ../orionbelt-semantic-layer && git describe --tags --always)"
git push

echo "✅ Docs deployed! Will be live at https://ralforion.com/orionbelt-semantic-layer/ in ~1 minute"