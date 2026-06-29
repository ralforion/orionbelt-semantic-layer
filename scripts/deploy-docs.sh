#!/usr/bin/env bash
# Build MkDocs site and deploy to gh-pages branch.
# Usage: ./scripts/deploy-docs.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Installing docs dependencies..."
uv sync --project "$REPO_ROOT" --extra docs

echo "Building docs..."
uv run --project "$REPO_ROOT" mkdocs build --strict --config-file "$REPO_ROOT/mkdocs.yml"

FILE_COUNT=$(find "$REPO_ROOT/site" -type f | wc -l | tr -d ' ')
echo "Built $FILE_COUNT files"

# Cache-bust custom CSS based on its content hash so style changes propagate
# to repeat visitors without a manual hard-refresh. The query string changes
# only when the CSS content actually changes, so caching still works between
# deploys with no CSS edits.
CSS_FILE="$REPO_ROOT/site/stylesheets/extra.css"
if [ -f "$CSS_FILE" ]; then
  CSS_HASH=$(shasum -a 256 "$CSS_FILE" | cut -c1-10)
  echo "Cache-busting extra.css (v=$CSS_HASH)..."
  # perl -i works the same on macOS and Linux (unlike sed -i).
  # Regex matches first-time references and any prior ?v=<hash>.
  find "$REPO_ROOT/site" -name "*.html" -exec \
    perl -i -pe "s|stylesheets/extra\\.css(\\?v=[a-f0-9]+)?|stylesheets/extra.css?v=$CSS_HASH|g" {} +
fi

# Deploy site/ to gh-pages branch preserving existing content
echo "Deploying to gh-pages branch (subdirectory only)..."

# Create temp directory for deployment
TEMP_DEPLOY=$(mktemp -d)
trap "rm -rf $TEMP_DEPLOY" EXIT

# Clone the gh-pages branch
git clone --depth 1 --branch gh-pages \
  "https://github.com/ralfbecher/ralfbecher.github.io.git" \
  "$TEMP_DEPLOY" 2>/dev/null || {
    echo "Creating new gh-pages branch..."
    mkdir -p "$TEMP_DEPLOY"
    cd "$TEMP_DEPLOY"
    git init
    git checkout -b gh-pages
    cd -
  }

# Remove old docs subdirectory, preserve everything else
rm -rf "$TEMP_DEPLOY/orionbelt-semantic-layer"

# Copy new docs to subdirectory
cp -r "$REPO_ROOT/site" "$TEMP_DEPLOY/orionbelt-semantic-layer"

# Ensure CNAME file exists for custom domain
echo "ralforion.com" > "$TEMP_DEPLOY/CNAME"

# Commit and push changes
cd "$TEMP_DEPLOY"
git add -A
git config user.name "github-actions"
git config user.email "github-actions@github.com"
git commit -m "Update OrionBelt docs ($(date +%Y-%m-%d))" || echo "No changes to commit"

# Push to gh-pages (requires authentication)
if [ -n "${GITHUB_TOKEN:-}" ]; then
  git push "https://x-access-token:${GITHUB_TOKEN}@github.com/ralfbecher/ralfbecher.github.io.git" gh-pages --force
else
  echo "Warning: GITHUB_TOKEN not set, cannot push. Run with:"
  echo "  GITHUB_TOKEN=your_token ./scripts/deploy-docs.sh"
fi

cd "$REPO_ROOT"
rm -rf "$REPO_ROOT/site"
echo "Done — docs deployed to gh-pages subdirectory (site/ cleaned up)."
