#!/usr/bin/env bash
# Coverage test for the nomic_multimodal_local pipeline.
# Exits non-zero on any failure. Prints a PASS/FAIL table at the end.
set -u

MCP_KEY="086c36ad2bcdc3199242d3d04c8fc5da1b909e78cfe32ac294ab6039e7403469"
MCP="http://localhost:8002"
ALEPH="http://localhost:8765"
NOMIC="http://localhost:8091"

PASS=0; FAIL=0
results=()

ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); results+=("PASS  $1"); }
fail() { echo "  ✗ $1  ---  $2"; FAIL=$((FAIL+1)); results+=("FAIL  $1 :: $2"); }

section() { echo; echo "=== $1 ==="; }

# ------------------------------------------------------------------
section "1. Host Nomic server"
# ------------------------------------------------------------------
H=$(curl -sf $NOMIC/health 2>&1) && {
    DIM=$(echo "$H" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("dim"))')
    DEV=$(echo "$H" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("device"))')
    [ "$DIM" = "768" ] && ok "health reports dim=768 device=$DEV" || fail "dim" "expected 768 got $DIM"
} || fail "health reachable" "$H"

TV=$(curl -sf $NOMIC/embed/text -H 'content-type: application/json' -d '{"text":"candlestick"}' | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["vector"]))')
[ "$TV" = "768" ] && ok "POST /embed/text returns 768-dim" || fail "/embed/text" "got dim=$TV"

# Pick a real keyframe id from the current DB (UUIDs change after reset).
KEYFRAME_ID=$(docker compose exec -T db psql -U aleph -d aleph_memory -At -c "SELECT id FROM memories WHERE kind='image' AND metadata->>'origin'='video_keyframe' LIMIT 1" 2>/dev/null | tr -d ' ')
curl -sf -o /tmp/test.jpg "$ALEPH/aleph/api/media/$KEYFRAME_ID" >/dev/null 2>&1
if [ -s /tmp/test.jpg ]; then
    IV=$(curl -sf -F "file=@/tmp/test.jpg" $NOMIC/embed/image | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["vector"]))')
    [ "$IV" = "768" ] && ok "POST /embed/image returns 768-dim" || fail "/embed/image" "got dim=$IV"
else
    fail "fetch keyframe for image test" "aleph /media failed"
fi

# ------------------------------------------------------------------
section "2. MCP health + counts"
# ------------------------------------------------------------------
H=$(curl -sf -H "Authorization: Bearer $MCP_KEY" $MCP/health)
MC=$(echo "$H" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("memory_count",0))')
[ "$MC" -gt 3000 ] && ok "memory_count=$MC (>3000)" || fail "memory_count" "only $MC"

STATE=$(echo "$H" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ingest"]["state"])')
[ "$STATE" = "idle" ] && ok "ingest state=idle" || fail "ingest state" "$STATE"

# ------------------------------------------------------------------
section "3. Kind coverage (DB-direct, 10 kinds expected)"
# ------------------------------------------------------------------
KINDS=$(docker compose exec -T db psql -U aleph -d aleph_memory -At -c "SELECT kind FROM memories GROUP BY kind ORDER BY kind" 2>/dev/null | tr '\n' ' ')
echo "  DB kinds present: $KINDS"
for k in doc_chunk image video_transcript pdf_text; do
    echo "$KINDS" | grep -qw "$k" && ok "kind '$k' present" || fail "kind '$k'" "missing"
done

# ------------------------------------------------------------------
section "4. search() — unified (kind=None)"
# ------------------------------------------------------------------
R=$(docker compose exec -T mcp python - <<'PY'
import asyncio, sys, json
sys.path.insert(0, '/app/mcp')
from memory import store, db
async def go():
    await db.init_pool()
    r = await store.search("volume profile histogram", limit=20)
    kinds = {}
    for x in r: kinds[x['kind']] = kinds.get(x['kind'],0)+1
    print(json.dumps({"count":len(r), "kinds":kinds, "top": r[0]['score'] if r else 0}))
asyncio.run(go())
PY
)
N=$(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin)["count"])')
[ "$N" -gt 10 ] && ok "unified search returns $N hits" || fail "unified count" "$N"
echo "  kind mix: $(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin)["kinds"])')"

# ------------------------------------------------------------------
section "5. search(kind='image') — visual hits surface (min_score auto)"
# ------------------------------------------------------------------
R=$(docker compose exec -T mcp python - <<'PY'
import asyncio, sys, json
sys.path.insert(0, '/app/mcp')
from memory import store, db
async def go():
    await db.init_pool()
    r = await store.search("volume profile histogram", kind="image", limit=5, min_score=0.05)
    print(json.dumps({"count":len(r), "top_score": r[0]['score'] if r else 0.0,
                      "top_src": (r[0].get('source_path') or '')[-60:] if r else ''}))
asyncio.run(go())
PY
)
N=$(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin)["count"])')
TOP=$(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin)["top_score"])')
SRC=$(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin)["top_src"])')
[ "$N" -gt 0 ] && ok "kind=image returns $N hits (top=$TOP from ...$SRC)" || fail "kind=image count" "0 hits (min_score filter?)"
python3 -c "exit(0 if $TOP > 0.08 else 1)" && ok "top image score $TOP > 0.08 (cross-modal discrimination)" || fail "cross-modal discrimination" "top score $TOP too low"

# ------------------------------------------------------------------
section "6. search(kind='video_transcript') — course content"
# ------------------------------------------------------------------
R=$(docker compose exec -T mcp python - <<'PY'
import asyncio, sys, json
sys.path.insert(0, '/app/mcp')
from memory import store, db
async def go():
    await db.init_pool()
    r = await store.search("strategie di trading volume profile", kind="video_transcript", limit=5)
    print(json.dumps({"count":len(r),
                      "top_score": r[0]['score'] if r else 0,
                      "top": r[0]['content'][:60] if r else ''}))
asyncio.run(go())
PY
)
N=$(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin)["count"])')
TOP=$(echo "$R" | python3 -c 'import json,sys;print(json.load(sys.stdin)["top_score"])')
[ "$N" -gt 0 ] && ok "kind=video_transcript returns $N hits (top=$TOP)" || fail "video_transcript count" "$N"
python3 -c "exit(0 if $TOP > 0.5 else 1)" && ok "transcript score $TOP > 0.5 (text-text retrieval strong)" || fail "text-text retrieval" "top score $TOP weak"

# ------------------------------------------------------------------
section "7. aleph /media keyframe serve — JPEG not video bytes"
# ------------------------------------------------------------------
IMG_ID=$(docker compose exec -T db psql -U aleph -d aleph_memory -At -c "SELECT id FROM memories WHERE kind='image' AND metadata->>'origin'='video_keyframe' LIMIT 1" 2>/dev/null | tr -d ' ')
if [ -n "$IMG_ID" ]; then
    CT=$(curl -so /dev/null -w '%{content_type}' "$ALEPH/aleph/api/media/$IMG_ID")
    SZ=$(curl -so /dev/null -w '%{size_download}' "$ALEPH/aleph/api/media/$IMG_ID")
    echo "  content-type=$CT size=$SZ"
    echo "$CT" | grep -iq 'image/jpeg' && ok "keyframe media serves image/jpeg ($SZ bytes)" || fail "media ctype" "got: $CT"
else
    fail "find a video_keyframe" "none in DB"
fi

# ------------------------------------------------------------------
section "8. tool registry — search present, search_docs absent"
# ------------------------------------------------------------------
R=$(docker compose exec -T mcp python - <<'PY'
import sys; sys.path.insert(0, '/app/mcp')
import importlib
# Walk the registered tools via the FastMCP object
try:
    import server as srv
    mcp_obj = getattr(srv, 'mcp', None)
    # FastMCP stores tools in a _tool_manager or similar
    names = []
    for attr in ('_tools', 'tools', '_tool_manager'):
        v = getattr(mcp_obj, attr, None)
        if v is not None:
            if hasattr(v, '_tools'):
                names = list(v._tools.keys()) if hasattr(v._tools, 'keys') else []
            elif hasattr(v, 'keys'):
                names = list(v.keys())
            break
    if not names:
        import asyncio
        async def _list():
            return await mcp_obj.list_tools()
        names = [t.name for t in asyncio.run(_list())]
    print(",".join(sorted(names)))
except Exception as e:
    print(f"ERR: {e}")
PY
)
echo "  registered tools: $R"
echo "$R" | grep -qw 'search' && ok "'search' is registered" || fail "search registered" "$R"
echo "$R" | grep -qw 'search_docs' && fail "search_docs retired" "still present" || ok "'search_docs' no longer exposed"

# ------------------------------------------------------------------
section "9. fetch_image full_res — video keyframe re-extract"
# ------------------------------------------------------------------
KF_ID=$(docker compose exec -T db psql -U aleph -d aleph_memory -At -c "SELECT id FROM memories WHERE kind='image' AND metadata->>'origin'='video_keyframe' LIMIT 1" 2>/dev/null | tr -d ' \r')
if [ -n "$KF_ID" ]; then
    OUT=$(docker compose exec -T mcp python - <<PY 2>/dev/null
import asyncio, sys
sys.path.insert(0, '/app/mcp')
from memory import db
from tools import memory as tm
captured = {}
class C:
    def tool(self, *a, **kw):
        def d(f):
            captured[f.__name__]=f; return f
        return d
tm.register(C())
async def go():
    await db.init_pool()
    fn = captured['fetch_image']
    thumb = await fn('$KF_ID', full_res=False)
    full  = await fn('$KF_ID', full_res=True)
    t = next((len(i.data) for i in thumb if hasattr(i,'data')), 0)
    f = next((len(i.data) for i in full if hasattr(i,'data')), 0)
    print(f"{t} {f}")
asyncio.run(go())
PY
)
    T=$(echo "$OUT" | awk '{print $1}')
    F=$(echo "$OUT" | awk '{print $2}')
    echo "  thumbnail=$T bytes   full_res=$F bytes"
    [ "${T:-0}" -gt 0 ] && [ "${T:-0}" -lt 30000 ] && ok "thumbnail returns ${T}B (≤30KB as expected)" || fail "thumbnail size" "$T"
    [ "${F:-0}" -gt 100000 ] && ok "full_res returns ${F}B (>100KB, re-extracted from video)" || fail "full_res size" "$F too small — re-extract failed?"
else
    fail "find keyframe for full_res test" "no video_keyframe in DB"
fi

# ------------------------------------------------------------------
section "10. aleph viewer + graph snapshot"
# ------------------------------------------------------------------
SN=$(docker compose exec -T db psql -U aleph -d aleph_memory -At -F' ' -c "SELECT version, jsonb_array_length(payload->'nodes') FROM graph_snapshot ORDER BY version DESC LIMIT 1" 2>/dev/null | tr -d '\r')
V=$(echo "$SN" | awk '{print $1}')
NODES=$(echo "$SN" | awk '{print $2}')
[ "${V:-0}" -ge 3 ] && [ "${NODES:-0}" -gt 3000 ] && ok "graph_snapshot v$V with $NODES nodes" || fail "snapshot" "v=$V nodes=$NODES"

# ------------------------------------------------------------------
echo
echo "=============================="
echo "  $PASS passed   $FAIL failed"
echo "=============================="
for r in "${results[@]}"; do echo "  $r"; done
exit $FAIL
