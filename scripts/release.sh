#!/usr/bin/env bash
# Public release pipeline for OrionBelt Semantic Layer.
#
# Usage: ./scripts/release.sh [--yes|-y] [--post-merge] [--from N] [--only N[,M,...]] [VERSION]
#
# Publishes everything that lives in the public repo: it merges the release PR,
# cuts the GitHub release (whose tag triggers both the Docker Hub and the PyPI
# publish workflows). Docs are deployed by the ralforion.github.io Actions workflow;
# the Cloud Run deploy is intentionally
# NOT here — that is an infra concern; the private infra release wrapper runs
# this script and then deploys to Cloud Run.
#
# If VERSION is not provided, reads it from pyproject.toml.
# If --yes (or env RELEASE_YES=1) is set, every confirmation auto-accepts —
# the pipeline runs straight through. Use with care: this kicks off PR
# merge, GitHub release, PyPI publish, and docs deploy without further
# interaction.
#
# If --post-merge (or env RELEASE_POST_MERGE=1) is set, the PR has already
# been merged out-of-band: the script must be run from main, Step 1 is
# skipped, and the run picks up at Step 2 (GitHub release). Guarded so
# we never re-release: the version's tag must not yet exist and the
# matching CHANGELOG entry must already be present.
#
# Step selection (mutually exclusive):
#   --from N        run steps N..4 (e.g. --from 3 resumes at PyPI publish)
#   --only N[,M,..] run only the listed steps (e.g. --only 4 redeploys docs)
# When step 1 is not selected, the run behaves like --post-merge: it must
# be on main and the PR is assumed already merged. The re-release tag guard
# only applies when step 2 (GitHub release) is part of the run, so partial
# reruns against an already-tagged version are allowed. Tests + ruff are
# skipped unless step 1 (PR merge) or step 3 (PyPI publish) is selected.
#
# Steps (each with confirmation prompt):
#   1. Create & merge PR (fix/ or feature/ branch → main) [skipped under --post-merge]
#   2. Create GitHub release with changelog (tag triggers the publish workflows)
#   3. Publish to PyPI (informational — the tag from Step 2 triggers the workflow)
#   4. Docs deploy note (handled by the ralforion.github.io Actions workflow)
#
# Docker images and PyPI packages are built and pushed by
# .github/workflows/docker-publish.yml and .github/workflows/pypi-publish.yml,
# both triggered by the vX.Y.Z tag created in Step 2 — there is no local Docker
# build or PyPI publish here.
#
# Prerequisites:
#   - gh CLI authenticated
#   - uv available (build + publish + docs)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

step() { echo -e "\n${CYAN}═══ $1 ═══${NC}"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

AUTO_YES="${RELEASE_YES:-0}"
POST_MERGE="${RELEASE_POST_MERGE:-0}"

confirm() {
    local prompt="${1:-Continue?}"
    if [[ "$AUTO_YES" == "1" ]]; then
        echo -e "${YELLOW}${prompt} [y/N]${NC} ${GREEN}y${NC} (auto)"
        return 0
    fi
    echo -en "${YELLOW}${prompt} [y/N] ${NC}"
    read -r answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "Skipped."; return 1; }
}

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
VERSION=""
FROM_STEP=""
ONLY_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) AUTO_YES=1 ;;
        --post-merge) POST_MERGE=1 ;;
        -h|--help)
            sed -n '2,45p' "$0"
            exit 0
            ;;
        --from)    shift; FROM_STEP="${1:-}" ;;
        --from=*)  FROM_STEP="${1#*=}" ;;
        --only)    shift; ONLY_ARG="${1:-}" ;;
        --only=*)  ONLY_ARG="${1#*=}" ;;
        -*) fail "Unknown option: $1" ;;
        *) VERSION="$1" ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Step selection (--from / --only)
# ---------------------------------------------------------------------------
# ONLY_SET is normalised to a ",N,N," string so membership is a simple glob
# (",$n," substring test). should_run() is the single gate every step block
# consults; with neither flag set it returns true for all steps.
TOTAL_STEPS=4
ONLY_SET=""
if [[ -n "$FROM_STEP" && -n "$ONLY_ARG" ]]; then
    fail "--from and --only are mutually exclusive."
fi
if [[ -n "$FROM_STEP" ]]; then
    [[ "$FROM_STEP" =~ ^[1-4]$ ]] || fail "--from: step must be 1-${TOTAL_STEPS} (got: $FROM_STEP)"
fi
if [[ -n "$ONLY_ARG" ]]; then
    IFS=',' read -ra _only_steps <<< "$ONLY_ARG"
    for s in "${_only_steps[@]}"; do
        [[ "$s" =~ ^[1-4]$ ]] || fail "--only: step must be 1-${TOTAL_STEPS} (got: $s)"
        ONLY_SET="${ONLY_SET}${s},"
    done
    ONLY_SET=",${ONLY_SET}"   # -> ",1,3,"
fi

should_run() {
    local n="$1"
    if [[ -n "$ONLY_SET" ]]; then
        [[ "$ONLY_SET" == *",$n,"* ]]
        return
    fi
    if [[ -n "$FROM_STEP" ]]; then
        (( n >= FROM_STEP ))
        return
    fi
    return 0
}

# Step 1 (create & merge PR) is skipped when --post-merge is set OR when the
# selection excludes it. In both cases the run operates on an already-merged
# main, so the branch guard and the uv.lock push target follow RUN_PR_STEP.
RUN_PR_STEP=1
if [[ "$POST_MERGE" == "1" ]] || ! should_run 1; then
    RUN_PR_STEP=0
fi

if [[ -z "$VERSION" ]]; then
    VERSION=$(grep '^version' "$REPO_ROOT/pyproject.toml" | head -1 | sed 's/.*"\(.*\)".*/\1/')
fi
if [[ -n "$FROM_STEP" ]]; then
    echo -e "${YELLOW}--from ${FROM_STEP}: running steps ${FROM_STEP}-${TOTAL_STEPS}.${NC}"
fi
if [[ -n "$ONLY_ARG" ]]; then
    echo -e "${YELLOW}--only ${ONLY_ARG}: running only the listed step(s).${NC}"
fi
if [[ "$AUTO_YES" == "1" ]]; then
    echo -e "${YELLOW}--yes mode: every confirmation auto-accepts.${NC}"
fi
if [[ "$POST_MERGE" == "1" ]]; then
    echo -e "${YELLOW}--post-merge mode: PR is already merged; Step 1 will be skipped.${NC}"
fi
echo -e "Release version: ${GREEN}v${VERSION}${NC}"

BRANCH=$(git branch --show-current)
echo -e "Current branch:  ${GREEN}${BRANCH}${NC}"

if [[ "$RUN_PR_STEP" == "0" ]]; then
    # PR step is not part of this run (--post-merge, or step 1 excluded by
    # --from/--only). Either way HEAD must already be on main.
    if [[ "$BRANCH" != "main" ]]; then
        fail "Step 1 (PR merge) is not selected, so this run must be on main (got: $BRANCH). Merge the PR first, or include step 1."
    fi
    # Re-release guard: only when step 2 (GitHub release) is in this run.
    # Partial reruns (e.g. --only 4) target an already-tagged version, so the
    # tag is expected to exist and must not abort them.
    if should_run 2; then
        if git rev-parse "v${VERSION}" >/dev/null 2>&1; then
            fail "tag v${VERSION} already exists locally — nothing to release (skip step 2 to redeploy)."
        fi
        if git ls-remote --exit-code --tags origin "v${VERSION}" >/dev/null 2>&1; then
            fail "tag v${VERSION} already exists on origin — nothing to release (skip step 2 to redeploy)."
        fi
    fi
    # The CHANGELOG entry must exist (proof the version-bump landed).
    if ! grep -q "## \[$VERSION\]" "$REPO_ROOT/CHANGELOG.md"; then
        fail "CHANGELOG.md missing [$VERSION] entry — version bump not landed?"
    fi
elif [[ "$BRANCH" == "main" ]]; then
    fail "You are on main. Switch to a fix/ or feature/ branch first (or pass --post-merge if the PR is already merged)."
fi

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
step "Pre-flight checks"

command -v gh >/dev/null || fail "gh CLI not found"
command -v uv >/dev/null || fail "uv not found"

if ! git diff --quiet; then
    fail "Uncommitted changes. Commit or stash first."
fi

# Tests + ruff guard the PR merge and the PyPI publish. For partial runs
# that touch neither (e.g. --only 4 redeploy docs), they already passed when
# the release landed — skip them so retries are fast.
if [[ "$RUN_PR_STEP" == "1" ]] || should_run 3; then
    echo "Running tests..."
    uv run pytest --tb=short -q || fail "Tests failed"
    ok "All tests pass"

    echo "Running ruff check..."
    uv run ruff check src/ || fail "Ruff check failed"
    ok "Ruff clean"
else
    warn "Skipping tests + ruff (no PR merge or PyPI publish in this run)"
fi

echo "Checking version consistency..."
PYPROJECT_VER=$(grep '^version' "$REPO_ROOT/pyproject.toml" | head -1 | sed 's/.*"\(.*\)".*/\1/')
INIT_VER=$(grep '__version__' "$REPO_ROOT/src/orionbelt/__init__.py" | sed 's/.*"\(.*\)".*/\1/')
[[ "$PYPROJECT_VER" == "$VERSION" ]] || fail "pyproject.toml version ($PYPROJECT_VER) != $VERSION"
[[ "$INIT_VER" == "$VERSION" ]]      || fail "__init__.py version ($INIT_VER) != $VERSION"
ok "Version consistent (pyproject.toml, __init__.py)"

echo "Checking CHANGELOG..."
grep -q "## \[$VERSION\]" "$REPO_ROOT/CHANGELOG.md" \
    || fail "CHANGELOG.md missing [$VERSION] entry"
grep -q "v$VERSION" "$REPO_ROOT/CHANGELOG-versions.md" \
    || fail "CHANGELOG-versions.md missing v$VERSION row"
ok "CHANGELOG entries present"

# ---------------------------------------------------------------------------
# Normalise uv.lock after pre-flight
# ---------------------------------------------------------------------------
# ``uv run pytest`` / ``uv run ruff check`` above can rewrite ``uv.lock``
# — most commonly when the project version was bumped in pyproject.toml
# but the developer didn't run ``uv sync`` themselves first. Pre-fix
# (v2.7.4 release): the checkout right after the squash-merge failed with
# "Your local changes to the following files would be overwritten by
# checkout: uv.lock", leaving the release half-done. Auto-commit and push so
# the branch / main stays consistent and the post-merge checkout has nothing
# dirty to trip on.
if ! git diff --quiet -- uv.lock 2>/dev/null; then
    warn "uv.lock was modified by pre-flight (likely uv sync after version bump)"
    git add uv.lock
    git commit -m "chore(release): refresh uv.lock for v${VERSION}" >/dev/null
    if [[ "$RUN_PR_STEP" == "0" ]]; then
        # On main — push directly. The PR is already merged (or step 1 was
        # not selected) so there is no branch to update; this is an addendum.
        git push origin main
    else
        # On the feature branch — push so the PR picks up the lock
        # update before merge. ``gh pr merge --squash`` below will block
        # until CI re-runs.
        git push origin "$BRANCH"
        echo "Lock-file commit pushed to $BRANCH — waiting briefly for CI to start..."
        sleep 10
    fi
    ok "uv.lock auto-committed and pushed"
fi

# ---------------------------------------------------------------------------
# 1. Create & merge PR
# ---------------------------------------------------------------------------
if [[ "$RUN_PR_STEP" == "0" ]]; then
    if [[ "$POST_MERGE" == "1" ]]; then
        step "1/4  Create & merge PR  [skipped — --post-merge]"
    else
        step "1/4  Create & merge PR  [skipped — not selected]"
    fi
    ok "PR already merged; current HEAD is on main"
else
    step "1/4  Create & merge PR"

    if ! git ls-remote --exit-code origin "$BRANCH" >/dev/null 2>&1; then
        echo "Pushing branch to origin..."
        git push -u origin "$BRANCH"
    fi

    EXISTING_PR=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number // empty' 2>/dev/null || true)

    if [[ -n "$EXISTING_PR" ]]; then
        echo "PR #${EXISTING_PR} already exists for branch ${BRANCH}"
        PR_URL=$(gh pr view "$EXISTING_PR" --json url --jq '.url')
        PR_NUMBER="$EXISTING_PR"
    else
        echo "Creating PR..."
        CHANGELOG=$(git log main..HEAD --oneline --no-merges)
        PR_URL=$(gh pr create \
            --title "Release v${VERSION}" \
            --body "$(cat <<EOF
## Summary
Release v${VERSION}

## Changes
\`\`\`
${CHANGELOG}
\`\`\`

## Release checklist
- [ ] Tests pass
- [ ] Ruff clean
- [ ] Version bumped in all locations
EOF
)" 2>&1 | tail -1)
        ok "PR created: $PR_URL"
        # Extract the trailing number from https://github.com/.../pull/NN
        PR_NUMBER="${PR_URL##*/}"
    fi

    if confirm "Merge PR into main?"; then
        # Repo policy: merge commits disallowed — squash is the convention
        # (see prior PRs ending in "(#NN)" on main).
        gh pr merge "$BRANCH" --squash --delete-branch
        # Backstop: if anything between pre-flight and now dirtied
        # ``uv.lock`` (the pre-flight normaliser above handles the
        # common case but a second ``uv run`` could re-dirty it), drop
        # the local copy — the squashed commit on main is the truth.
        if ! git diff --quiet -- uv.lock 2>/dev/null; then
            warn "uv.lock dirty pre-checkout — discarding (main has the truth)"
            git checkout -- uv.lock
        fi
        git checkout main
        git pull origin main
        ok "PR merged (squash) and branch deleted"

        # Auto-close any GitHub issues referenced in the merged PR body.
        # GitHub's built-in auto-close only fires when the PR body uses
        # the ``Closes #N`` / ``Fixes #N`` / ``Resolves #N`` keywords on
        # their own line — table-format references like ``| #88 | foo |``
        # don't trigger it. This pass parses *any* ``#N`` reference in
        # the PR body and closes still-open issues with a release-version
        # comment. Idempotent: already-closed issues / non-issue numbers
        # (PRs, SQL examples) are silently skipped.
        if [[ -n "${PR_NUMBER:-}" ]]; then
            pr_body=$(gh pr view "$PR_NUMBER" --json body --jq .body 2>/dev/null || true)
            if [[ -n "$pr_body" ]]; then
                # Extract unique #N references, strip the # prefix. The
                # trailing ``|| true`` keeps a no-match grep (PR bodies without
                # any ``#N`` reference) from aborting the run under ``set -e``.
                referenced=$(grep -oE '#[0-9]+' <<<"$pr_body" | sort -u | tr -d '#' || true)
                for n in $referenced; do
                    # Skip the PR itself
                    [[ "$n" == "$PR_NUMBER" ]] && continue
                    state=$(gh issue view "$n" --json state --jq .state 2>/dev/null || true)
                    if [[ "$state" == "OPEN" ]]; then
                        gh issue close "$n" --reason completed \
                            --comment "Shipped in v${VERSION} (PR #${PR_NUMBER})." \
                            >/dev/null 2>&1 \
                            && ok "Closed issue #$n (referenced in PR #${PR_NUMBER})"
                    fi
                done
            fi
        fi

        BRANCH="main"
    else
        warn "PR not merged — remaining steps require main. Aborting."
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# 2. GitHub release  (tag triggers the Docker Hub publish workflow)
# ---------------------------------------------------------------------------
if ! should_run 2; then
    step "2/4  Create GitHub release  [skipped — not selected]"
else
step "2/4  Create GitHub release"

TAG="v${VERSION}"
if git tag -l "$TAG" | grep -q "$TAG"; then
    warn "Tag $TAG already exists"
else
    if confirm "Create GitHub release $TAG?"; then
        PREV_TAG=$(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)
        NOTES_FILE=$(mktemp)

        # Pull the matching ``## [VERSION]`` section out of CHANGELOG.md.
        # Using ``git log --oneline`` here used to be enough, but with the
        # current squash-merge convention every PR collapses to a single
        # commit on main — so the release notes ended up as a one-liner.
        # The CHANGELOG entry already curates Added/Changed/Fixed sections
        # for the release; ship that verbatim.
        CHANGELOG_FILE="$REPO_ROOT/CHANGELOG.md"
        CHANGELOG_SECTION=""
        if [[ -f "$CHANGELOG_FILE" ]]; then
            CHANGELOG_SECTION=$(awk -v ver="$VERSION" '
                $0 ~ "^## \\[" ver "\\]" { capturing=1; print; next }
                capturing && /^## \[/    { exit }
                capturing                 { print }
            ' "$CHANGELOG_FILE")
        fi

        if [[ -n "$CHANGELOG_SECTION" ]]; then
            {
                printf '%s\n\n' "$CHANGELOG_SECTION"
                printf '**Full Changelog**: https://github.com/ralforion/orionbelt-semantic-layer/compare/%s...%s\n' \
                    "$PREV_TAG" "$TAG"
            } >"$NOTES_FILE"
        else
            warn "CHANGELOG.md missing [$VERSION] section — falling back to git log"
            NOTES=$(git log "${PREV_TAG}"..HEAD --oneline --no-merges | head -20)
            # Build notes via printf into a temp file — avoids the bash heredoc-in-$()
            # apostrophe quirk that triggers "unexpected EOF while looking for `''".
            {
                printf '## Changes\n\n%s\n\n' "$NOTES"
                printf '**Full Changelog**: https://github.com/ralforion/orionbelt-semantic-layer/compare/%s...%s\n' \
                    "$PREV_TAG" "$TAG"
            } >"$NOTES_FILE"
        fi

        gh release create "$TAG" \
            --title "v${VERSION}" \
            --notes-file "$NOTES_FILE"
        rm -f "$NOTES_FILE"
        ok "GitHub release $TAG created"
        ok "Tag $TAG pushed — the Docker Hub publish workflow will build and push the images"
    fi
fi
fi

# ---------------------------------------------------------------------------
# 3. Publish to PyPI
# ---------------------------------------------------------------------------
# PyPI publishing is handled by .github/workflows/pypi-publish.yml, which is
# triggered by the vX.Y.Z tag created in Step 2 (GitHub release) - the same
# model as the Docker Hub publish workflow. It builds osi-orionbelt first
# (skip-existing, since it has an independent version) and then the main
# package, using PyPI Trusted Publishing (OIDC) - no token is stored or used
# here. There is no local build/publish in this script.
if ! should_run 3; then
    step "3/4  Publish to PyPI  [skipped — not selected]"
else
step "3/4  Publish to PyPI"
ok "PyPI publish is triggered by the v${VERSION} tag via .github/workflows/pypi-publish.yml"
echo "  Watch: https://github.com/ralforion/orionbelt-semantic-layer/actions/workflows/pypi-publish.yml"
echo "  (Requires Trusted Publishers configured on PyPI for both projects.)"
fi

# ---------------------------------------------------------------------------
# 4. Deploy MkDocs
# ---------------------------------------------------------------------------
if ! should_run 4; then
    step "4/4  Deploy MkDocs to gh-pages  [skipped — not selected]"
else
step "4/4  Docs deploy (handled by ralforion.github.io Actions)"

# Docs are deployed by the ralforion.github.io repo's GitHub Actions workflow
# (.github/workflows/deploy.yml), not from here — deploying from two places
# force-pushes the same gh-pages branch and conflicts (see #168). Update the
# orionbelt-semantic-layer docs in ralforion.github.io and push its main (or
# run its Deploy workflow) to publish.
ok "Docs deploy is handled by the ralforion.github.io GitHub Actions workflow"
echo "  Repo:  https://github.com/ralforion/ralforion.github.io"
echo "  Site:  https://ralforion.com/orionbelt-semantic-layer/"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo -e "${GREEN}  Public release v${VERSION} complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo ""
echo "Published:"
echo "  PyPI:      https://pypi.org/project/orionbelt-semantic-layer/${VERSION}/"
echo "  Docs:      https://ralforion.com/orionbelt-semantic-layer/"
echo "  Docker:    https://hub.docker.com/r/ralforion/orionbelt-semantic-layer-api (built by the publish workflow)"
echo "  Actions:   https://github.com/ralforion/orionbelt-semantic-layer/actions/workflows/docker-publish.yml"
