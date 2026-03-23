#!/usr/bin/env bash
# Automated Docker image tests for OrionBelt API.
# Usage: ./tests/docker/test_docker.sh [--no-build]
#
# Builds the image (unless --no-build), starts a container,
# runs endpoint tests, and reports results.

set -euo pipefail

IMAGE_NAME="orionbelt-api"
CONTAINER_NAME="orionbelt-docker-test"
HOST_PORT=18080
MAX_WAIT=30  # seconds to wait for startup
PASSED=0
FAILED=0
TESTS=()

# ── Helpers ──────────────────────────────────────────────────────────

cleanup() {
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}

log() { printf "\033[1m%s\033[0m\n" "$*"; }
pass() { PASSED=$((PASSED + 1)); TESTS+=("PASS: $1"); printf "  \033[32mPASS\033[0m %s\n" "$1"; }
fail() { FAILED=$((FAILED + 1)); TESTS+=("FAIL: $1 — $2"); printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "$2"; }

BASE_URL="http://localhost:${HOST_PORT}"

# GET/POST helper — sets $BODY and $HTTP_CODE
api() {
    local method=$1 path=$2
    shift 2
    local response
    response=$(curl -s -w "\n%{http_code}" -X "$method" "${BASE_URL}${path}" "$@")
    HTTP_CODE=$(echo "$response" | tail -1)
    BODY=$(echo "$response" | sed '$d')
}

json_field() {
    python3 -c "import sys,json; print(json.loads(sys.stdin.read())$1)" <<< "$BODY"
}

# ── Build ────────────────────────────────────────────────────────────

trap cleanup EXIT

if [[ "${1:-}" != "--no-build" ]]; then
    log "Building Docker image..."
    docker build -t "$IMAGE_NAME" . >/dev/null 2>&1
    log "Build complete."
else
    log "Skipping build (--no-build)."
fi

# ── Start container ──────────────────────────────────────────────────

cleanup
log "Starting container on port ${HOST_PORT}..."
docker run -d --name "$CONTAINER_NAME" -p "${HOST_PORT}:8080" "$IMAGE_NAME" >/dev/null

# Wait for health endpoint
log "Waiting for startup..."
elapsed=0
while ! curl -sf "${BASE_URL}/health" >/dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [[ $elapsed -ge $MAX_WAIT ]]; then
        fail "startup" "Container did not become healthy within ${MAX_WAIT}s"
        echo ""
        log "Container logs:"
        docker logs "$CONTAINER_NAME"
        exit 1
    fi
done
log "Container ready (${elapsed}s)."
echo ""

# ── Tests ────────────────────────────────────────────────────────────

log "Running tests..."
echo ""

# 1. Health endpoint
api GET /health
if [[ "$HTTP_CODE" == "200" ]] && [[ "$(json_field "['status']")" == "ok" ]]; then
    pass "GET /health returns 200 with status=ok"
else
    fail "GET /health" "HTTP $HTTP_CODE, body: $BODY"
fi

# 2. Version present in health
VERSION=$(json_field "['version']" 2>/dev/null || echo "")
if [[ -n "$VERSION" && "$VERSION" != "None" ]]; then
    pass "GET /health includes version ($VERSION)"
else
    fail "GET /health version" "missing version field"
fi

# 3. Dialects endpoint
api GET /v1/dialects
DIALECT_COUNT=$(python3 -c "import sys,json; print(len(json.loads(sys.stdin.read())['dialects']))" <<< "$BODY")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$DIALECT_COUNT" -ge 5 ]]; then
    pass "GET /dialects returns $DIALECT_COUNT dialects"
else
    fail "GET /dialects" "HTTP $HTTP_CODE, count=$DIALECT_COUNT"
fi

# 4. Create session
api POST /v1/sessions
SESSION_ID=$(json_field "['session_id']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]] && [[ -n "$SESSION_ID" ]]; then
    pass "POST /sessions creates session ($SESSION_ID)"
else
    fail "POST /sessions" "HTTP $HTTP_CODE, body: $BODY"
fi

# 5. List sessions (may be disabled via DISABLE_SESSION_LIST=true in Docker)
api GET /v1/sessions
if [[ "$HTTP_CODE" == "200" ]]; then
    SESSION_COUNT=$(python3 -c "import sys,json; print(len(json.loads(sys.stdin.read())['sessions']))" <<< "$BODY")
    if [[ "$SESSION_COUNT" -ge 1 ]]; then
        pass "GET /sessions lists $SESSION_COUNT session(s)"
    else
        fail "GET /sessions" "HTTP $HTTP_CODE, count=$SESSION_COUNT"
    fi
elif [[ "$HTTP_CODE" == "403" ]]; then
    pass "GET /sessions disabled (DISABLE_SESSION_LIST=true)"
else
    fail "GET /sessions" "HTTP $HTTP_CODE"
fi

# 6. Get session
api GET "/v1/sessions/${SESSION_ID}"
if [[ "$HTTP_CODE" == "200" ]] && [[ "$(json_field "['session_id']")" == "$SESSION_ID" ]]; then
    pass "GET /sessions/{id} returns session details"
else
    fail "GET /sessions/{id}" "HTTP $HTTP_CODE"
fi

# 7. Load model
MODEL_YAML=$(python3 -c "import json; print(json.dumps(open('examples/sem-layer.obml.yml').read()))")
api POST "/v1/sessions/${SESSION_ID}/models" \
    -H "Content-Type: application/json" \
    -d "{\"model_id\": \"test\", \"model_yaml\": ${MODEL_YAML}}"
MODEL_ID=$(json_field "['model_id']" 2>/dev/null || echo "")
OBJ_COUNT=$(json_field "['data_objects']" 2>/dev/null || echo "0")
if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]] && [[ "$OBJ_COUNT" -ge 1 ]]; then
    pass "POST /sessions/{id}/models loads model ($OBJ_COUNT data objects)"
else
    fail "POST /sessions/{id}/models" "HTTP $HTTP_CODE, body: $BODY"
fi

# 8. List models
api GET "/v1/sessions/${SESSION_ID}/models"
if [[ "$HTTP_CODE" == "200" ]]; then
    pass "GET /sessions/{id}/models lists models"
else
    fail "GET /sessions/{id}/models" "HTTP $HTTP_CODE"
fi

# 9. Describe model
api GET "/v1/sessions/${SESSION_ID}/models/${MODEL_ID}"
DESC_OBJS=$(python3 -c "import sys,json; print(len(json.loads(sys.stdin.read())['data_objects']))" <<< "$BODY" 2>/dev/null || echo "0")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$DESC_OBJS" -ge 1 ]]; then
    pass "GET /sessions/{id}/models/{mid} describes model ($DESC_OBJS data objects)"
else
    fail "GET /sessions/{id}/models/{mid}" "HTTP $HTTP_CODE"
fi

# 10. Compile query (star schema — single fact)
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d "{
        \"model_id\": \"${MODEL_ID}\",
        \"dialect\": \"postgres\",
        \"query\": {
            \"select\": {
                \"dimensions\": [\"Product Category\", \"Client Gender\"],
                \"measures\": [\"Total Sales\", \"Sales Count\"]
            }
        }
    }"
SQL=$(json_field "['sql']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$SQL" == *"SELECT"* ]]; then
    pass "POST query/sql compiles star-schema query (postgres)"
else
    fail "POST query/sql star-schema" "HTTP $HTTP_CODE, body: $BODY"
fi

# 11. Compile query — different dialect (snowflake)
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d "{
        \"model_id\": \"${MODEL_ID}\",
        \"dialect\": \"snowflake\",
        \"query\": {
            \"select\": {
                \"dimensions\": [\"Product Category\"],
                \"measures\": [\"Total Sales\"]
            }
        }
    }"
SQL=$(json_field "['sql']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$SQL" == *"SELECT"* ]]; then
    pass "POST query/sql compiles query (snowflake)"
else
    fail "POST query/sql snowflake" "HTTP $HTTP_CODE, body: $BODY"
fi

# 12. Validate model
api POST "/v1/sessions/${SESSION_ID}/validate" \
    -H "Content-Type: application/json" \
    -d "{\"model_yaml\": ${MODEL_YAML}}"
if [[ "$HTTP_CODE" == "200" ]]; then
    pass "POST /sessions/{id}/validate validates model"
else
    fail "POST /sessions/{id}/validate" "HTTP $HTTP_CODE, body: $BODY"
fi

# 13. Cumulative metric query (running total)
CUMUL_QUERY='{
    "select": {
        "dimensions": ["Sales Date"],
        "measures": ["Running Total Sales"]
    }
}'
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d "{
        \"model_id\": \"${MODEL_ID}\",
        \"dialect\": \"postgres\",
        \"query\": ${CUMUL_QUERY}
    }"
SQL=$(json_field "['sql']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$SQL" == *"cumulative_base"* ]]; then
    pass "POST query/sql compiles cumulative metric (running total)"
else
    fail "POST query/sql cumulative" "HTTP $HTTP_CODE, body: $BODY"
fi

# 14. Cumulative metric query (rolling window)
ROLLING_QUERY='{
    "select": {
        "dimensions": ["Sales Date"],
        "measures": ["Rolling 3m Sales"]
    }
}'
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d "{
        \"model_id\": \"${MODEL_ID}\",
        \"dialect\": \"postgres\",
        \"query\": ${ROLLING_QUERY}
    }"
SQL=$(json_field "['sql']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$SQL" == *"cumulative_base"* ]] && [[ "$SQL" == *"ROWS BETWEEN"* ]]; then
    pass "POST query/sql compiles cumulative metric (rolling 3m)"
else
    fail "POST query/sql rolling cumulative" "HTTP $HTTP_CODE, body: $BODY"
fi

# 15. Period-over-Period metric query (MoM change)
POP_QUERY='{
    "select": {
        "dimensions": ["Sales Date"],
        "measures": ["Sales MoM Change"]
    }
}'
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d "{
        \"model_id\": \"${MODEL_ID}\",
        \"dialect\": \"postgres\",
        \"query\": ${POP_QUERY}
    }"
SQL=$(json_field "['sql']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$SQL" == *"date_range"* ]] && [[ "$SQL" == *"date_spine"* ]] && [[ "$SQL" == *"pop_base"* ]]; then
    pass "POST query/sql compiles PoP metric (MoM change)"
else
    fail "POST query/sql PoP" "HTTP $HTTP_CODE, body: $BODY"
fi

# 16. Filtered measure query (CASE WHEN)
FILTERED_QUERY='{
    "select": {
        "dimensions": ["Product Category"],
        "measures": ["Electronics Sales"]
    }
}'
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d "{
        \"model_id\": \"${MODEL_ID}\",
        \"dialect\": \"postgres\",
        \"query\": ${FILTERED_QUERY}
    }"
SQL=$(json_field "['sql']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$SQL" == *"CASE WHEN"* ]]; then
    pass "POST query/sql compiles filtered measure (CASE WHEN)"
else
    fail "POST query/sql filtered measure" "HTTP $HTTP_CODE, body: $BODY"
fi

# 17. Ratio metric with filtered component
RATIO_QUERY='{
    "select": {
        "dimensions": ["Product Category"],
        "measures": ["Electronics Share"]
    }
}'
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d "{
        \"model_id\": \"${MODEL_ID}\",
        \"dialect\": \"postgres\",
        \"query\": ${RATIO_QUERY}
    }"
SQL=$(json_field "['sql']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$SQL" == *"CASE WHEN"* ]] && [[ "$SQL" == *"SUM"* ]]; then
    pass "POST query/sql compiles ratio metric with filtered component"
else
    fail "POST query/sql ratio metric" "HTTP $HTTP_CODE, body: $BODY"
fi

# 18. Invalid query returns error (unknown dimension)
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d "{
        \"model_id\": \"${MODEL_ID}\",
        \"dialect\": \"postgres\",
        \"query\": {
            \"select\": {
                \"dimensions\": [\"NonExistent\"],
                \"measures\": [\"Total Sales\"]
            }
        }
    }"
if [[ "$HTTP_CODE" == "400" ]] || [[ "$HTTP_CODE" == "422" ]]; then
    pass "POST query/sql rejects unknown dimension (HTTP $HTTP_CODE)"
else
    fail "POST query/sql error handling" "expected 4xx, got HTTP $HTTP_CODE"
fi

# 14. Delete model
api DELETE "/v1/sessions/${SESSION_ID}/models/${MODEL_ID}"
if [[ "$HTTP_CODE" == "200" ]] || [[ "$HTTP_CODE" == "204" ]]; then
    pass "DELETE /sessions/{id}/models/{mid} removes model"
else
    fail "DELETE model" "HTTP $HTTP_CODE"
fi

# 15. Delete session
api DELETE "/v1/sessions/${SESSION_ID}"
if [[ "$HTTP_CODE" == "200" ]] || [[ "$HTTP_CODE" == "204" ]]; then
    pass "DELETE /sessions/{id} closes session"
else
    fail "DELETE session" "HTTP $HTTP_CODE"
fi

# ── Summary ──────────────────────────────────────────────────────────

echo ""
log "Results: ${PASSED} passed, ${FAILED} failed ($(( PASSED + FAILED )) total)"

if [[ $FAILED -gt 0 ]]; then
    echo ""
    for t in "${TESTS[@]}"; do
        if [[ "$t" == FAIL* ]]; then
            printf "  \033[31m%s\033[0m\n" "$t"
        fi
    done
    exit 1
fi

exit 0
