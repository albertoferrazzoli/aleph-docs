#!/usr/bin/env bash
# Smoke test for multi-workspace switching.
# Assumes workspaces.yaml defines at least 'trading_course' and 'scratch'.
set -u

MCP_KEY="${MCP_API_KEY:-086c36ad2bcdc3199242d3d04c8fc5da1b909e78cfe32ac294ab6039e7403469}"
ALEPH_KEY="${ALEPH_API_KEY:-336554c6a7e53e230bfd5a63ff00d0d49e810110fb32193ab0788603de3a68cd}"
MCP="http://localhost:8002"
ALEPH="http://localhost:8765"

PASS=0; FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1  ---  $2"; FAIL=$((FAIL+1)); }
section() { echo; echo "=== $1 ==="; }

# ------------------------------------------------------------------
section "1. Aleph lists workspaces"
# ------------------------------------------------------------------
R=$(curl -s "$ALEPH/aleph/api/workspaces")
ACTIVE=$(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("active",""))')
NAMES=$(echo "$R" | python3 -c 'import json,sys;print(" ".join(w["name"] for w in json.load(sys.stdin).get("workspaces",[])))')
echo "  active=$ACTIVE"
echo "  workspaces=$NAMES"
[ -n "$ACTIVE" ] && ok "active workspace is set" || fail "active unset" "$R"
echo "$NAMES" | grep -qw trading_course && ok "'trading_course' listed" || fail "missing trading_course" "$NAMES"
echo "$NAMES" | grep -qw scratch && ok "'scratch' listed" || fail "missing scratch" "$NAMES"

# ------------------------------------------------------------------
section "2. Switch aleph → scratch (expect empty DB)"
# ------------------------------------------------------------------
R=$(curl -s -X POST "$ALEPH/aleph/api/workspaces/active" \
    -H "Content-Type: application/json" \
    -H "X-Aleph-Key: $ALEPH_KEY" \
    -d '{"name":"scratch","reindex":false}')
DB=$(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("pg_db",""))')
[ "$DB" = "aleph_scratch" ] && ok "aleph reports pg_db=aleph_scratch" || fail "bad pg_db" "$R"

# Give the watcher one cycle (polls every 5s)
sleep 7

MC=$(curl -s -H "Authorization: Bearer $MCP_KEY" "$MCP/health" \
     | python3 -c 'import json,sys;print(json.load(sys.stdin).get("memory_count",-1))')
[ "$MC" = "0" ] && ok "MCP watcher synced: memory_count=0" || fail "MCP did not sync" "memory_count=$MC"

# ------------------------------------------------------------------
section "3. Switch aleph → trading_course (expect >3000 memories)"
# ------------------------------------------------------------------
R=$(curl -s -X POST "$ALEPH/aleph/api/workspaces/active" \
    -H "Content-Type: application/json" \
    -H "X-Aleph-Key: $ALEPH_KEY" \
    -d '{"name":"trading_course","reindex":false}')
DB=$(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("pg_db",""))')
[ "$DB" = "aleph_memory" ] && ok "aleph reports pg_db=aleph_memory" || fail "bad pg_db" "$R"

sleep 7

MC=$(curl -s -H "Authorization: Bearer $MCP_KEY" "$MCP/health" \
     | python3 -c 'import json,sys;print(json.load(sys.stdin).get("memory_count",-1))')
[ "$MC" -gt 3000 ] && ok "MCP re-synced: memory_count=$MC (>3000)" || fail "MCP lost sync" "memory_count=$MC"

# ------------------------------------------------------------------
section "4. Reject unknown workspace"
# ------------------------------------------------------------------
CODE=$(curl -s -o /tmp/switch_err.json -w '%{http_code}' -X POST \
    "$ALEPH/aleph/api/workspaces/active" \
    -H "Content-Type: application/json" \
    -H "X-Aleph-Key: $ALEPH_KEY" \
    -d '{"name":"bogus","reindex":false}')
[ "$CODE" = "404" ] && ok "unknown workspace returns 404" || fail "unknown workspace" "HTTP $CODE"

echo
echo "=============================="
echo "  $PASS passed   $FAIL failed"
echo "=============================="
exit $FAIL
