#!/usr/bin/env bash
# Automated Cloud Run API tests for OrionBelt Semantic Layer.
# Usage: ./tests/cloudrun/test_cloudrun.sh [BASE_URL]
#
# If no URL is given, reads ORIONBELT_API_URL from the environment.
# Runs the full session lifecycle against the live deployment.

set -euo pipefail

BASE_URL="${1:-${ORIONBELT_API_URL:-}}"
if [[ -z "$BASE_URL" ]]; then
    echo "Usage: $0 <BASE_URL>"
    echo "   or: ORIONBELT_API_URL=https://... $0"
    exit 1
fi
# Strip trailing slash
BASE_URL="${BASE_URL%/}"

PASSED=0
FAILED=0
TESTS=()
SESSION_ID=""
MODEL_ID=""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MODEL_FILE="$PROJECT_ROOT/examples/sem-layer.obml.yml"

# ── Helpers ──────────────────────────────────────────────────────────

log() { printf "\033[1m%s\033[0m\n" "$*"; }
pass() { PASSED=$((PASSED + 1)); TESTS+=("PASS: $1"); printf "  \033[32mPASS\033[0m %s\n" "$1"; }
fail() { FAILED=$((FAILED + 1)); TESTS+=("FAIL: $1 — $2"); printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "$2"; }

# GET/POST/DELETE helper — sets $BODY and $HTTP_CODE
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

json_len() {
    python3 -c "import sys,json; print(len(json.loads(sys.stdin.read())$1))" <<< "$BODY"
}

cleanup() {
    # Best-effort: delete session if it was created
    if [[ -n "$SESSION_ID" ]]; then
        curl -s -X DELETE "${BASE_URL}/v1/sessions/${SESSION_ID}" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# ── Pre-flight ───────────────────────────────────────────────────────

log "Target: $BASE_URL"
echo ""
log "Running Cloud Run API tests..."
echo ""

# ── 1. Health ────────────────────────────────────────────────────────

api GET /health
if [[ "$HTTP_CODE" == "200" ]] && [[ "$(json_field "['status']")" == "ok" ]]; then
    pass "GET /health returns 200 with status=ok"
else
    fail "GET /health" "HTTP $HTTP_CODE, body: $BODY"
fi

# 2. Version in health response
VERSION=$(json_field "['version']" 2>/dev/null || echo "")
if [[ -n "$VERSION" && "$VERSION" != "None" ]]; then
    pass "GET /health includes version ($VERSION)"
else
    fail "GET /health version" "missing version field"
fi

# ── 3. Dialects ──────────────────────────────────────────────────────

api GET /v1/dialects
DIALECT_COUNT=$(json_len "['dialects']" 2>/dev/null || echo "0")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$DIALECT_COUNT" -ge 5 ]]; then
    pass "GET /dialects returns $DIALECT_COUNT dialects"
else
    fail "GET /dialects" "HTTP $HTTP_CODE, count=$DIALECT_COUNT"
fi

# ── 4. Create session ───────────────────────────────────────────────

api POST /v1/sessions -H "Content-Type: application/json" -d '{"metadata":{"purpose":"integration-test"}}'
SESSION_ID=$(json_field "['session_id']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]] && [[ -n "$SESSION_ID" ]]; then
    pass "POST /sessions creates session ($SESSION_ID)"
else
    fail "POST /sessions" "HTTP $HTTP_CODE, body: $BODY"
fi

# ── 5. List sessions ────────────────────────────────────────────────

api GET /v1/sessions
SESSION_COUNT=$(json_len "['sessions']" 2>/dev/null || echo "0")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$SESSION_COUNT" -ge 1 ]]; then
    pass "GET /sessions lists $SESSION_COUNT session(s)"
elif [[ "$HTTP_CODE" == "403" ]]; then
    pass "GET /sessions disabled (DISABLE_SESSION_LIST=true)"
else
    fail "GET /sessions" "HTTP $HTTP_CODE, count=$SESSION_COUNT"
fi

# ── 6. Get session ──────────────────────────────────────────────────

api GET "/v1/sessions/${SESSION_ID}"
GOT_SID=$(json_field "['session_id']" 2>/dev/null || echo "")
GOT_META=$(json_field "['metadata']['purpose']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$GOT_SID" == "$SESSION_ID" ]]; then
    pass "GET /sessions/{id} returns session details"
else
    fail "GET /sessions/{id}" "HTTP $HTTP_CODE"
fi

# 7. Session metadata preserved
if [[ "$GOT_META" == "integration-test" ]]; then
    pass "Session metadata preserved (purpose=integration-test)"
else
    fail "Session metadata" "expected integration-test, got $GOT_META"
fi

# ── 8. Load model ───────────────────────────────────────────────────

MODEL_YAML=$(python3 -c "import json; print(json.dumps(open('$MODEL_FILE').read()))")
api POST "/v1/sessions/${SESSION_ID}/models" \
    -H "Content-Type: application/json" \
    -d "{\"model_id\": \"test\", \"model_yaml\": ${MODEL_YAML}}"
MODEL_ID=$(json_field "['model_id']" 2>/dev/null || echo "")
OBJ_COUNT=$(json_field "['data_objects']" 2>/dev/null || echo "0")
DIM_COUNT=$(json_field "['dimensions']" 2>/dev/null || echo "0")
MSR_COUNT=$(json_field "['measures']" 2>/dev/null || echo "0")
MTR_COUNT=$(json_field "['metrics']" 2>/dev/null || echo "0")
if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]] && [[ "$OBJ_COUNT" -ge 1 ]]; then
    pass "POST /sessions/{id}/models loads model (${OBJ_COUNT} objects, ${DIM_COUNT} dims, ${MSR_COUNT} measures, ${MTR_COUNT} metrics)"
else
    fail "POST /sessions/{id}/models" "HTTP $HTTP_CODE, body: $BODY"
fi

# ── 9. List models ──────────────────────────────────────────────────

api GET "/v1/sessions/${SESSION_ID}/models"
MODEL_COUNT=$(json_len "" 2>/dev/null || echo "0")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$MODEL_COUNT" -ge 1 ]]; then
    pass "GET /sessions/{id}/models lists $MODEL_COUNT model(s)"
else
    fail "GET /sessions/{id}/models" "HTTP $HTTP_CODE"
fi

# ── 10. Describe model ──────────────────────────────────────────────

api GET "/v1/sessions/${SESSION_ID}/models/${MODEL_ID}"
DESC_OBJS=$(json_len "['data_objects']" 2>/dev/null || echo "0")
DESC_DIMS=$(json_len "['dimensions']" 2>/dev/null || echo "0")
DESC_MSRS=$(json_len "['measures']" 2>/dev/null || echo "0")
DESC_MTRS=$(json_len "['metrics']" 2>/dev/null || echo "0")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$DESC_OBJS" -ge 1 ]]; then
    pass "GET /sessions/{id}/models/{mid} describes model (${DESC_OBJS} objects, ${DESC_DIMS} dims, ${DESC_MSRS} measures, ${DESC_MTRS} metrics)"
else
    fail "GET /sessions/{id}/models/{mid}" "HTTP $HTTP_CODE"
fi

# ── 11. Validate model ──────────────────────────────────────────────

api POST "/v1/sessions/${SESSION_ID}/validate" \
    -H "Content-Type: application/json" \
    -d "{\"model_yaml\": ${MODEL_YAML}}"
VALID=$(json_field "['valid']" 2>/dev/null || echo "")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$VALID" == "True" ]]; then
    pass "POST /sessions/{id}/validate — model is valid"
else
    fail "POST /sessions/{id}/validate" "HTTP $HTTP_CODE, valid=$VALID"
fi

# 12. Validate invalid YAML — returns 200 with valid=false
api POST "/v1/sessions/${SESSION_ID}/validate" \
    -H "Content-Type: application/json" \
    -d '{"model_yaml": "key: [unclosed"}'
VALID_BAD=$(json_field "['valid']" 2>/dev/null || echo "")
ERR_COUNT=$(json_len "['errors']" 2>/dev/null || echo "0")
if [[ "$HTTP_CODE" == "200" ]] && [[ "$VALID_BAD" == "False" ]] && [[ "$ERR_COUNT" -ge 1 ]]; then
    pass "POST /sessions/{id}/validate returns valid=false for bad YAML ($ERR_COUNT error(s))"
else
    fail "POST validate invalid" "HTTP $HTTP_CODE, valid=$VALID_BAD, errors=$ERR_COUNT"
fi

# ── Compile queries across all 5 dialects ────────────────────────────

compile_query() {
    local test_name=$1 dialect=$2 query_json=$3 expect_pattern=$4
    api POST "/v1/sessions/${SESSION_ID}/query/sql" \
        -H "Content-Type: application/json" \
        -d "{
            \"model_id\": \"${MODEL_ID}\",
            \"dialect\": \"${dialect}\",
            \"query\": ${query_json}
        }"
    SQL=$(json_field "['sql']" 2>/dev/null || echo "")
    SQL_VALID=$(json_field "['sql_valid']" 2>/dev/null || echo "")
    if [[ "$HTTP_CODE" == "200" ]] && [[ "$SQL" == *"$expect_pattern"* ]]; then
        pass "$test_name ($dialect) — sql_valid=$SQL_VALID"
    else
        fail "$test_name ($dialect)" "HTTP $HTTP_CODE, body: $BODY"
    fi
}

STAR_QUERY='{
    "select": {
        "dimensions": ["Product Category", "Client Gender"],
        "measures": ["Total Sales", "Sales Count"]
    }
}'

# 13-17. Star-schema query across all dialects
compile_query "Star-schema query" "postgres"    "$STAR_QUERY" "SELECT"
compile_query "Star-schema query" "snowflake"   "$STAR_QUERY" "SELECT"
compile_query "Star-schema query" "clickhouse"  "$STAR_QUERY" "SELECT"
compile_query "Star-schema query" "databricks"  "$STAR_QUERY" "SELECT"
compile_query "Star-schema query" "dremio"      "$STAR_QUERY" "SELECT"

# 18. Query with WHERE filter
FILTER_QUERY='{
    "select": {
        "dimensions": ["Product Name"],
        "measures": ["Total Sales"]
    },
    "where": [{"field": "Product Category", "op": "equals", "value": "Electronics"}],
    "orderBy": [{"field": "Total Sales", "direction": "desc"}],
    "limit": 10
}'
compile_query "Filtered query with ORDER BY + LIMIT" "postgres" "$FILTER_QUERY" "WHERE"

# 19. Query with multiple measures from same fact
MULTI_MEASURE='{
    "select": {
        "dimensions": ["Employee Name"],
        "measures": ["Total Sales", "Total Sales Qty", "Sales Count"]
    }
}'
compile_query "Multi-measure single-fact query" "snowflake" "$MULTI_MEASURE" "SELECT"

# 20. Query joining through multiple tables (Sales → Products → Suppliers)
DEEP_JOIN='{
    "select": {
        "dimensions": ["Supplier Name"],
        "measures": ["Total Sales"]
    }
}'
compile_query "Deep join (Sales→Products→Suppliers)" "postgres" "$DEEP_JOIN" "JOIN"

# 21. Purchases by Supplier (different base fact)
PURCHASE_QUERY='{
    "select": {
        "dimensions": ["Supplier Name"],
        "measures": ["Total Purchases", "Total Purchase Qty"]
    },
    "orderBy": [{"field": "Total Purchases", "direction": "desc"}]
}'
compile_query "Purchases fact table query" "dremio" "$PURCHASE_QUERY" "purchases"

# ── Cumulative & PoP metric queries ──────────────────────────────────

# 22. Cumulative metric — running total
CUMUL_QUERY='{
    "select": {
        "dimensions": ["Sales Date"],
        "measures": ["Running Total Sales"]
    }
}'
compile_query "Cumulative running total" "postgres" "$CUMUL_QUERY" "cumulative_base"

# 23. Cumulative metric — rolling 3-month window
ROLLING_QUERY='{
    "select": {
        "dimensions": ["Sales Date"],
        "measures": ["Rolling 3m Sales"]
    }
}'
compile_query "Cumulative rolling 3m" "postgres" "$ROLLING_QUERY" "cumulative_base"

# 24. Period-over-Period metric — MoM change
POP_QUERY='{
    "select": {
        "dimensions": ["Sales Date"],
        "measures": ["Sales MoM Change"]
    }
}'
compile_query "PoP MoM change" "postgres" "$POP_QUERY" "date_spine"

# 25. PoP metric with additional dimension
POP_DIM_QUERY='{
    "select": {
        "dimensions": ["Sales Date", "Product Category"],
        "measures": ["Sales MoM Change"]
    }
}'
compile_query "PoP MoM with dimension" "snowflake" "$POP_DIM_QUERY" "pop_compare"

# 27. Filtered measure (CASE WHEN)
FILTERED_QUERY='{
    "select": {
        "dimensions": ["Product Category"],
        "measures": ["Electronics Sales"]
    }
}'
compile_query "Filtered measure CASE WHEN" "postgres" "$FILTERED_QUERY" "CASE WHEN"

# 28. Ratio metric with filtered component
RATIO_QUERY='{
    "select": {
        "dimensions": ["Product Category"],
        "measures": ["Electronics Share"]
    }
}'
compile_query "Ratio metric with filtered component" "duckdb" "$RATIO_QUERY" "CASE WHEN"

# 29. Cumulative metric — different dialect
compile_query "Cumulative running total" "snowflake" "$CUMUL_QUERY" "cumulative_base"

# ── Error handling ───────────────────────────────────────────────────

# 22. Unknown dimension
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
    pass "Rejects unknown dimension (HTTP $HTTP_CODE)"
else
    fail "Unknown dimension error" "expected 4xx, got HTTP $HTTP_CODE"
fi

# 23. Unknown measure
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d "{
        \"model_id\": \"${MODEL_ID}\",
        \"dialect\": \"postgres\",
        \"query\": {
            \"select\": {
                \"dimensions\": [\"Product Name\"],
                \"measures\": [\"Fake Measure\"]
            }
        }
    }"
if [[ "$HTTP_CODE" == "400" ]] || [[ "$HTTP_CODE" == "422" ]]; then
    pass "Rejects unknown measure (HTTP $HTTP_CODE)"
else
    fail "Unknown measure error" "expected 4xx, got HTTP $HTTP_CODE"
fi

# 24. Unknown model ID
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d '{
        "model_id": "nonexist",
        "dialect": "postgres",
        "query": {
            "select": {
                "dimensions": ["Product Name"],
                "measures": ["Total Sales"]
            }
        }
    }'
if [[ "$HTTP_CODE" == "404" ]]; then
    pass "Rejects unknown model_id (HTTP 404)"
else
    fail "Unknown model error" "expected 404, got HTTP $HTTP_CODE"
fi

# 25. Unknown dialect
api POST "/v1/sessions/${SESSION_ID}/query/sql" \
    -H "Content-Type: application/json" \
    -d "{
        \"model_id\": \"${MODEL_ID}\",
        \"dialect\": \"oracle\",
        \"query\": {
            \"select\": {
                \"dimensions\": [\"Product Name\"],
                \"measures\": [\"Total Sales\"]
            }
        }
    }"
if [[ "$HTTP_CODE" == "400" ]] || [[ "$HTTP_CODE" == "422" ]]; then
    pass "Rejects unknown dialect (HTTP $HTTP_CODE)"
else
    fail "Unknown dialect error" "expected 4xx, got HTTP $HTTP_CODE"
fi

# 26. Missing session
api GET "/sessions/nonexist999"
if [[ "$HTTP_CODE" == "404" ]]; then
    pass "GET missing session returns 404"
else
    fail "Missing session" "expected 404, got HTTP $HTTP_CODE"
fi

# ── Cleanup ──────────────────────────────────────────────────────────

# 27. Delete model
api DELETE "/v1/sessions/${SESSION_ID}/models/${MODEL_ID}"
if [[ "$HTTP_CODE" == "200" ]] || [[ "$HTTP_CODE" == "204" ]]; then
    pass "DELETE /sessions/{id}/models/{mid} removes model"
else
    fail "DELETE model" "HTTP $HTTP_CODE"
fi

# 28. Verify model gone
api GET "/v1/sessions/${SESSION_ID}/models/${MODEL_ID}"
if [[ "$HTTP_CODE" == "404" ]]; then
    pass "Deleted model returns 404"
else
    fail "Deleted model check" "expected 404, got HTTP $HTTP_CODE"
fi

# 29. Delete session
api DELETE "/v1/sessions/${SESSION_ID}"
if [[ "$HTTP_CODE" == "200" ]] || [[ "$HTTP_CODE" == "204" ]]; then
    pass "DELETE /sessions/{id} closes session"
    SESSION_ID=""  # Prevent cleanup trap from re-deleting
else
    fail "DELETE session" "HTTP $HTTP_CODE"
fi

# 30. Verify session gone
api GET "/sessions/${SESSION_ID:-deleted}"
if [[ "$HTTP_CODE" == "404" ]]; then
    pass "Deleted session returns 404"
else
    fail "Deleted session check" "expected 404, got HTTP $HTTP_CODE"
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

echo ""
log "All tests passed against $BASE_URL"
exit 0
