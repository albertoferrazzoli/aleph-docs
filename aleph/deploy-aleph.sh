#!/bin/bash
#
# Deploy the Aleph app (semantic memory viewer) to the production VM.
#
# Usage: ./deploy-aleph.sh [--skip-apache] [--skip-frontend] [--skip-pg] [-h]
#
# Idempotent. Safe to re-run. Preserves /opt/aleph/.env and /opt/aleph/data/.
# Backs up Apache config before editing and restores it if configtest fails.

set -euo pipefail

PROD_VM="<VM_NAME>"
PROD_ZONE="europe-west1-d"
PROD_PROJECT="<GCP_PROJECT>"

ALEPH_REMOTE_PATH="/opt/aleph-docs/aleph"
MCP_REMOTE_PATH="/opt/aleph-docs/mcp"
PG_DB_NAME="aleph_memory"
PG_ROLE_NAME="aleph"

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
ALEPH_LOCAL_PATH="$DEPLOY_DIR"
BACKEND_LOCAL="$DEPLOY_DIR/backend"
FRONTEND_LOCAL="$DEPLOY_DIR/frontend"
SYSTEMD_LOCAL="$DEPLOY_DIR/systemd"

APACHE_CONF="${APACHE_CONF:-/etc/apache2/sites-enabled/000-default.conf}"
# The htpasswd file now lives with the app (read by the backend via
# ALEPH_HTPASSWD_FILE env var). Apache is no longer in the auth loop.
HTPASSWD_FILE="${ALEPH_REMOTE_PATH}/data/htpasswd"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()   { echo -e "${GREEN}[aleph]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

SKIP_APACHE=false
SKIP_FRONTEND=false
SKIP_PG=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --skip-apache     Do not touch Apache config (code-only refresh).
  --skip-frontend   Do not rebuild / upload the frontend.
  --skip-pg         Do not apply schema_additions.sql / triggers.sql.
  -h, --help        Show this help and exit.

Reads from $DEPLOY_DIR/.env (or $MCP_REMOTE_PATH/.env on the VM for GOOGLE_API_KEY):
  ALEPH_API_KEY        (required)
  PG_PASSWORD          (required unless --skip-pg and .env already on VM)
  HTPASSWD_USER        (required unless --skip-apache)
  HTPASSWD_PASSWORD    (required unless --skip-apache)
  GOOGLE_API_KEY       (optional locally — falls back to MCP's .env on VM)
EOF
}

for arg in "$@"; do
    case "$arg" in
        --skip-apache)   SKIP_APACHE=true ;;
        --skip-frontend) SKIP_FRONTEND=true ;;
        --skip-pg)       SKIP_PG=true ;;
        -h|--help)       usage; exit 0 ;;
        *) error "Unknown argument: $arg (use --help)" ;;
    esac
done

# Pre-flight: verify source tree
[ -f "$BACKEND_LOCAL/main.py" ]           || error "backend/main.py missing at $BACKEND_LOCAL"
[ -f "$BACKEND_LOCAL/requirements.txt" ]  || error "backend/requirements.txt missing"
[ -f "$SYSTEMD_LOCAL/aleph-backend.service" ] || error "systemd/aleph-backend.service missing"
if [ "$SKIP_FRONTEND" = false ]; then
    [ -f "$FRONTEND_LOCAL/package.json" ] || error "frontend/package.json missing at $FRONTEND_LOCAL"
fi

echo ""
echo "=================================================="
echo "  DEPLOY Aleph (semantic memory viewer)"
echo "=================================================="
echo "  From: $ALEPH_LOCAL_PATH"
echo "  To:   $PROD_VM:$ALEPH_REMOTE_PATH"
echo "  URL:  https://example.com/aleph/"
echo "  Flags: skip-apache=$SKIP_APACHE skip-frontend=$SKIP_FRONTEND skip-pg=$SKIP_PG"
echo "=================================================="
echo ""

# ---------------------------------------------------------------------------
echo "--- [1/9] Read secrets from local .env"
# ---------------------------------------------------------------------------
ENV_FILE="$DEPLOY_DIR/.env"
_read_env_var() {
    [ -f "$ENV_FILE" ] || return 0
    grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

ALEPH_API_KEY_VAL=$(_read_env_var ALEPH_API_KEY)
[ -n "${ALEPH_API_KEY_VAL:-}" ] || error "ALEPH_API_KEY missing in $ENV_FILE"
log "ALEPH_API_KEY loaded (not shown)."

GOOGLE_API_KEY_VAL=$(_read_env_var GOOGLE_API_KEY)
if [ -n "${GOOGLE_API_KEY_VAL:-}" ]; then
    log "GOOGLE_API_KEY loaded from local .env (not shown)."
else
    log "GOOGLE_API_KEY not in local .env — will read remote $MCP_REMOTE_PATH/.env."
fi

PG_PASSWORD_VAL=""
if [ "$SKIP_PG" = false ]; then
    PG_PASSWORD_VAL=$(_read_env_var PG_PASSWORD)
    if [ -z "${PG_PASSWORD_VAL:-}" ]; then
        log "PG_PASSWORD not in local .env — will read remote $MCP_REMOTE_PATH/.env for PG_DSN."
    else
        log "PG_PASSWORD loaded (not shown)."
    fi
fi

HTPASSWD_USER_VAL=""
HTPASSWD_PASSWORD_VAL=""
if [ "$SKIP_APACHE" = false ]; then
    HTPASSWD_USER_VAL=$(_read_env_var HTPASSWD_USER)
    HTPASSWD_PASSWORD_VAL=$(_read_env_var HTPASSWD_PASSWORD)
    [ -n "${HTPASSWD_USER_VAL:-}" ]     || error "HTPASSWD_USER missing in $ENV_FILE (or use --skip-apache)"
    [ -n "${HTPASSWD_PASSWORD_VAL:-}" ] || error "HTPASSWD_PASSWORD missing in $ENV_FILE (or use --skip-apache)"
    log "HTPASSWD credentials loaded (not shown)."
fi

# ---------------------------------------------------------------------------
echo "--- [2/9] Frontend build"
# ---------------------------------------------------------------------------
FRONTEND_TAR=""
if [ "$SKIP_FRONTEND" = false ]; then
    log "Installing frontend deps + building..."
    ( cd "$FRONTEND_LOCAL" && npm install --silent && npm run build )
    [ -d "$FRONTEND_LOCAL/dist" ] || error "frontend build produced no dist/"
    FRONTEND_TAR=$(mktemp /tmp/aleph-frontend-dist-XXXXXX.tar.gz)
    COPYFILE_DISABLE=1 tar -czf "$FRONTEND_TAR" -C "$FRONTEND_LOCAL/dist" .
    log "Frontend archive: $FRONTEND_TAR ($(du -h "$FRONTEND_TAR" | cut -f1))"
else
    log "--skip-frontend: not rebuilding/uploading the SPA."
fi

# ---------------------------------------------------------------------------
echo "--- [3/9] Archive backend + systemd units"
# ---------------------------------------------------------------------------
BACKEND_TAR=$(mktemp /tmp/aleph-backend-XXXXXX.tar.gz)
COPYFILE_DISABLE=1 tar -czf "$BACKEND_TAR" \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.venv' \
    --exclude='tests/__pycache__' \
    --exclude='.pytest_cache' \
    -C "$DEPLOY_DIR" backend systemd
log "Backend archive: $BACKEND_TAR ($(du -h "$BACKEND_TAR" | cut -f1))"

# ---------------------------------------------------------------------------
echo "--- [4/9] Upload archives to VM"
# ---------------------------------------------------------------------------
log "Uploading backend tarball..."
gcloud compute scp "$BACKEND_TAR" "$PROD_VM:/tmp/aleph-backend.tar.gz" \
    --zone="$PROD_ZONE" --project="$PROD_PROJECT" --quiet
rm -f "$BACKEND_TAR"

if [ "$SKIP_FRONTEND" = false ]; then
    log "Uploading frontend tarball..."
    gcloud compute scp "$FRONTEND_TAR" "$PROD_VM:/tmp/aleph-frontend-dist.tar.gz" \
        --zone="$PROD_ZONE" --project="$PROD_PROJECT" --quiet
    rm -f "$FRONTEND_TAR"
fi

# ---------------------------------------------------------------------------
echo "--- [5/9] Install backend + systemd units on VM"
# ---------------------------------------------------------------------------
gcloud compute ssh "$PROD_VM" --zone="$PROD_ZONE" --project="$PROD_PROJECT" --command="
    set -euo pipefail
    ALEPH=$ALEPH_REMOTE_PATH
    SKIP_FRONTEND=$SKIP_FRONTEND

    sudo mkdir -p \"\$ALEPH\" \"\$ALEPH/data\"

    # Preserve .env and data/ across re-deploys
    if [ -f \"\$ALEPH/.env\" ]; then
        sudo cp \"\$ALEPH/.env\" /tmp/aleph-env-backup
    fi
    if [ -d \"\$ALEPH/data\" ]; then
        sudo cp -a \"\$ALEPH/data\" /tmp/aleph-data-backup
    fi

    # Wipe old backend/ and systemd/ but keep .env, data/, frontend/, .venv
    sudo rm -rf \"\$ALEPH/backend\" \"\$ALEPH/systemd\"
    sudo tar -xzf /tmp/aleph-backend.tar.gz -C \"\$ALEPH\"
    rm -f /tmp/aleph-backend.tar.gz

    # Restore preserved bits
    if [ -f /tmp/aleph-env-backup ]; then
        sudo mv /tmp/aleph-env-backup \"\$ALEPH/.env\"
    fi
    if [ -d /tmp/aleph-data-backup ]; then
        sudo mkdir -p \"\$ALEPH/data\"
        sudo cp -a /tmp/aleph-data-backup/. \"\$ALEPH/data/\"
        sudo rm -rf /tmp/aleph-data-backup
    fi

    # Frontend install
    if [ \"\$SKIP_FRONTEND\" = 'false' ]; then
        sudo rm -rf \"\$ALEPH/frontend/dist\"
        sudo mkdir -p \"\$ALEPH/frontend/dist\"
        sudo tar -xzf /tmp/aleph-frontend-dist.tar.gz -C \"\$ALEPH/frontend/dist\"
        rm -f /tmp/aleph-frontend-dist.tar.gz
    fi

    # Virtualenv + deps
    if [ ! -d \"\$ALEPH/.venv\" ]; then
        sudo python3 -m venv \"\$ALEPH/.venv\"
    fi
    sudo \"\$ALEPH/.venv/bin/pip\" install -r \"\$ALEPH/backend/requirements.txt\" --quiet

    sudo chown -R www-data:www-data \"\$ALEPH\"

    # Install systemd units from shipped systemd/
    sudo install -m 0644 \"\$ALEPH/systemd/aleph-backend.service\"      /etc/systemd/system/aleph-backend.service
    sudo install -m 0644 \"\$ALEPH/systemd/aleph-projection.service\"   /etc/systemd/system/aleph-projection.service
    sudo install -m 0644 \"\$ALEPH/systemd/aleph-projection.timer\"     /etc/systemd/system/aleph-projection.timer
"

# ---------------------------------------------------------------------------
echo "--- [6/9] Postgres schema + trigger"
# ---------------------------------------------------------------------------
if [ "$SKIP_PG" = false ]; then
    log "Applying schema_additions.sql + triggers.sql..."
    gcloud compute ssh "$PROD_VM" --zone="$PROD_ZONE" --project="$PROD_PROJECT" --command="
        set -euo pipefail
        ALEPH=$ALEPH_REMOTE_PATH
        DB=$PG_DB_NAME

        if [ -f \"\$ALEPH/backend/schema_additions.sql\" ]; then
            sudo -u postgres psql -v ON_ERROR_STOP=1 \"\$DB\" \\
                -f \"\$ALEPH/backend/schema_additions.sql\" >/dev/null
        else
            echo '[deploy] WARNING: backend/schema_additions.sql not found — skipping'
        fi

        if [ -f \"\$ALEPH/backend/triggers.sql\" ]; then
            sudo -u postgres psql -v ON_ERROR_STOP=1 \"\$DB\" \\
                -f \"\$ALEPH/backend/triggers.sql\" >/dev/null
        else
            echo '[deploy] WARNING: backend/triggers.sql not found — skipping'
        fi

        TRG=\$(sudo -u postgres psql -tAc \"SELECT 1 FROM pg_trigger WHERE tgname='memory_change_trg'\" \"\$DB\" || true)
        if [ \"\$TRG\" = '1' ]; then
            echo '[deploy] memory_change_trg is present'
        else
            echo '[deploy] WARNING: memory_change_trg not found after apply'
        fi

        # Grant app role access to the new table + all sequences (idempotent).
        sudo -u postgres psql -v ON_ERROR_STOP=1 \"\$DB\" >/dev/null <<'GRANTSQL'
GRANT ALL ON graph_snapshot TO aleph;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aleph;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO aleph;
GRANTSQL
        echo '[deploy] grants applied to aleph role'
    "
else
    log "--skip-pg: skipping schema + trigger apply."
fi

# ---------------------------------------------------------------------------
echo "--- [7/9] Upsert /opt/aleph/.env"
# ---------------------------------------------------------------------------
log "Writing /opt/aleph/.env (chmod 600, www-data:www-data)..."
gcloud compute ssh "$PROD_VM" --zone="$PROD_ZONE" --project="$PROD_PROJECT" --command="
    set -euo pipefail
    ALEPH=$ALEPH_REMOTE_PATH
    MCP=$MCP_REMOTE_PATH
    ENVF=\"\$ALEPH/.env\"

    if [ ! -f \"\$ENVF\" ]; then
        sudo touch \"\$ENVF\"
        sudo chown www-data:www-data \"\$ENVF\"
        sudo chmod 600 \"\$ENVF\"
    fi

    # Read a value from another .env (careful: file is root:root or www-data:www-data 600)
    _read_remote_env() {
        local file=\"\$1\" key=\"\$2\"
        [ -f \"\$file\" ] || { echo ''; return 0; }
        sudo grep -E \"^\${key}=\" \"\$file\" 2>/dev/null | head -1 | cut -d= -f2- \\
            | sed -e 's/^\"//' -e 's/\"\$//' -e \"s/^'//\" -e \"s/'\$//\"
    }

    LOCAL_GOOGLE='$GOOGLE_API_KEY_VAL'
    LOCAL_PG_PW='$PG_PASSWORD_VAL'

    if [ -z \"\$LOCAL_GOOGLE\" ]; then
        LOCAL_GOOGLE=\$(_read_remote_env \"\$MCP/.env\" GOOGLE_API_KEY)
    fi
    if [ -z \"\$LOCAL_PG_PW\" ]; then
        # Try to grab it from the remote aleph-docs-mcp DSN or PG_PASSWORD
        REMOTE_DSN=\$(_read_remote_env \"\$MCP/.env\" PG_DSN)
        if [ -n \"\$REMOTE_DSN\" ]; then
            # postgresql://user:pass@host:port/db
            LOCAL_PG_PW=\$(echo \"\$REMOTE_DSN\" | sed -n 's|^postgresql://[^:]*:\\([^@]*\\)@.*|\\1|p')
        fi
        if [ -z \"\$LOCAL_PG_PW\" ]; then
            LOCAL_PG_PW=\$(_read_remote_env \"\$MCP/.env\" PG_PASSWORD)
        fi
    fi

    upsert_env() {
        local key=\"\$1\" val=\"\$2\"
        if sudo grep -qE \"^\${key}=\" \"\$ENVF\"; then
            sudo sed -i.bak \"s|^\${key}=.*|\${key}=\${val}|\" \"\$ENVF\"
            sudo rm -f \"\${ENVF}.bak\"
        else
            echo \"\${key}=\${val}\" | sudo tee -a \"\$ENVF\" >/dev/null
        fi
    }

    upsert_env ALEPH_API_KEY              '$ALEPH_API_KEY_VAL'
    upsert_env ALEPH_HTPASSWD_FILE        '$HTPASSWD_FILE'
    upsert_env ALEPH_SESSIONS_DB          '$ALEPH_REMOTE_PATH/data/sessions.db'
    upsert_env ALEPH_SESSION_TTL_HOURS    '24'
    upsert_env ALEPH_COOKIE_SECURE        '1'
    upsert_env ALEPH_COOKIE_PATH          '/aleph'
    upsert_env MEMORY_ENABLED             'true'
    upsert_env MCP_PATH                   '$MCP_REMOTE_PATH'
    # EMBED_BACKEND must match the backend used by the MCP indexer —
    # query vectors and stored vectors must live in the same latent space.
    # Default to gemini-2-preview (multimodal); override via env if the MCP
    # side was bootstrapped with a text-only backend.
    upsert_env EMBED_BACKEND   "${EMBED_BACKEND_FROM_MCP:-gemini-2-preview}"
    upsert_env EMBED_DIM       '1536'
    upsert_env LOG_LEVEL       'INFO'
    upsert_env ALEPH_HOST      '127.0.0.1'
    upsert_env ALEPH_PORT      '8765'

    if [ -n \"\$LOCAL_GOOGLE\" ]; then
        upsert_env GOOGLE_API_KEY \"\$LOCAL_GOOGLE\"
    else
        echo '[deploy] WARNING: no GOOGLE_API_KEY available — remember() writes will fail until set'
    fi

    if [ -n \"\$LOCAL_PG_PW\" ]; then
        upsert_env PG_DSN \"postgresql://$PG_ROLE_NAME:\${LOCAL_PG_PW}@localhost:5432/$PG_DB_NAME\"
    else
        echo '[deploy] WARNING: no PG_PASSWORD available — PG_DSN not set; edit \$ENVF manually'
    fi

    sudo chown www-data:www-data \"\$ENVF\"
    sudo chmod 600 \"\$ENVF\"
"

# ---------------------------------------------------------------------------
echo "--- [8/9] Apache: /aleph alias + SPA fallback + reverse proxy"
# ---------------------------------------------------------------------------
# Note: auth is now enforced inside the backend (see backend/auth.py).
# Apache does pure reverse-proxying; the only reason we still touch the
# htpasswd file here is to SEED it on first deploy so the operator has a
# working login without shelling into the VM.
if [ "$SKIP_APACHE" = false ]; then
    log "Updating Apache config (idempotent, with backup + configtest)..."
    gcloud compute ssh "$PROD_VM" --zone="$PROD_ZONE" --project="$PROD_PROJECT" --command="
        set -euo pipefail
        CONF=$APACHE_CONF
        HTPASSWD=$HTPASSWD_FILE
        HTU='$HTPASSWD_USER_VAL'
        HTP='$HTPASSWD_PASSWORD_VAL'

        [ -f \"\$CONF\" ] || { echo \"[deploy] ERROR: \$CONF not found\"; exit 1; }

        # Ensure required Apache modules (auth_basic / authn_file no
        # longer required — auth runs inside the app).
        sudo a2enmod proxy proxy_http headers rewrite >/dev/null

        # Ensure htpasswd tool (used to seed the initial admin account)
        if ! command -v htpasswd >/dev/null 2>&1; then
            sudo DEBIAN_FRONTEND=noninteractive apt-get install -y apache2-utils >/dev/null
        fi

        # Seed the app-owned htpasswd. We only rewrite when the file is
        # MISSING or the operator supplied credentials — never clobber a
        # file that's been edited by hand on the VM.
        sudo mkdir -p \"\$(dirname \"\$HTPASSWD\")\"
        if [ ! -s \"\$HTPASSWD\" ] && [ -n \"\$HTU\" ] && [ -n \"\$HTP\" ]; then
            printf '%s' \"\$HTP\" | sudo htpasswd -ciB \"\$HTPASSWD\" \"\$HTU\" >/dev/null
            echo \"[deploy] htpasswd seeded for user '\$HTU' at \$HTPASSWD\"
        fi
        sudo chown www-data:www-data \"\$HTPASSWD\" 2>/dev/null || true
        sudo chmod 0640 \"\$HTPASSWD\" 2>/dev/null || true

        # Backup before any edit
        TS=\$(date +%Y%m%d-%H%M%S)
        BACKUP=\"\${CONF}.bak.\${TS}\"
        sudo cp -a \"\$CONF\" \"\$BACKUP\"
        echo \"[deploy] Backed up Apache conf to \$BACKUP\"

        # Write aleph block to a tmp file
        ALEPH_BLOCK=\$(mktemp /tmp/aleph-block-XXXXXX.conf)
        cat > \"\$ALEPH_BLOCK\" <<'ALEPHEOF'
    # --- ALEPH BEGIN ---
    Alias /aleph /opt/aleph/frontend/dist
    <Directory /opt/aleph/frontend/dist>
        Require all granted
        Options -Indexes +FollowSymLinks
        # SPA fallback: unknown paths serve index.html
        RewriteEngine On
        RewriteBase /aleph/
        RewriteCond %{REQUEST_FILENAME} !-f
        RewriteCond %{REQUEST_FILENAME} !-d
        RewriteCond %{REQUEST_URI} !^/aleph/api
        RewriteRule . /aleph/index.html [L]
    </Directory>
    # No Apache-level auth: the backend gates /aleph/api via session
    # cookies + bearer tokens (see backend/auth.py). Login page at
    # /aleph/login.html remains public so the user can obtain a session.
    # SSE: no buffering, long timeout (must come BEFORE the general /aleph/api ProxyPass)
    <Location /aleph/api/graph/stream>
        ProxyPass http://127.0.0.1:8765/graph/stream flushpackets=on keepalive=On timeout=86400
        ProxyPassReverse http://127.0.0.1:8765/graph/stream
        SetEnv proxy-sendchunked 1
        Header set X-Accel-Buffering \"no\"
    </Location>
    ProxyPass /aleph/api http://127.0.0.1:8765
    ProxyPassReverse /aleph/api http://127.0.0.1:8765
    # --- ALEPH END ---
ALEPHEOF

        # Inject or replace the marker block
        TMP=\$(sudo mktemp /tmp/wordpress-conf-XXXXXX)
        if sudo grep -q '# --- ALEPH BEGIN ---' \"\$CONF\"; then
            # Replace between markers (inclusive)
            sudo awk -v blockfile=\"\$ALEPH_BLOCK\" '
                BEGIN { skipping = 0 }
                /# --- ALEPH BEGIN ---/ {
                    while ((getline line < blockfile) > 0) print line
                    close(blockfile)
                    skipping = 1
                    next
                }
                /# --- ALEPH END ---/ { skipping = 0; next }
                { if (!skipping) print }
            ' \"\$CONF\" | sudo tee \"\$TMP\" >/dev/null
        else
            # Insert before the last </VirtualHost>
            sudo awk -v blockfile=\"\$ALEPH_BLOCK\" '
                { lines[NR] = \$0 }
                END {
                    # Find last line matching </VirtualHost>
                    last = 0
                    for (i = 1; i <= NR; i++) if (lines[i] ~ /<\\/VirtualHost>/) last = i
                    for (i = 1; i <= NR; i++) {
                        if (i == last) {
                            while ((getline l < blockfile) > 0) print l
                            close(blockfile)
                        }
                        print lines[i]
                    }
                }
            ' \"\$CONF\" | sudo tee \"\$TMP\" >/dev/null
        fi

        sudo mv \"\$TMP\" \"\$CONF\"
        sudo chown root:root \"\$CONF\"
        sudo chmod 0644 \"\$CONF\"
        rm -f \"\$ALEPH_BLOCK\"

        # Validate — restore backup on failure
        if ! sudo apache2ctl configtest 2>&1; then
            echo '[deploy] ERROR: apache2ctl configtest FAILED — restoring backup'
            sudo cp -a \"\$BACKUP\" \"\$CONF\"
            exit 1
        fi

        sudo systemctl reload apache2
        echo '[deploy] Apache reloaded OK'
    "
else
    log "--skip-apache: not touching Apache config."
fi

# ---------------------------------------------------------------------------
echo "--- [9/9] systemd: start services + health check"
# ---------------------------------------------------------------------------
gcloud compute ssh "$PROD_VM" --zone="$PROD_ZONE" --project="$PROD_PROJECT" --command="
    set -euo pipefail
    sudo systemctl daemon-reload
    sudo systemctl enable --now aleph-backend.service >/dev/null
    sudo systemctl enable --now aleph-projection.timer >/dev/null

    # aleph-backend needs to be alive (the service, not oneshot)
    sudo systemctl restart aleph-backend.service

    sleep 3
    if sudo systemctl is-active --quiet aleph-backend.service; then
        echo '[deploy] aleph-backend is active'
    else
        echo '[deploy] ERROR: aleph-backend is NOT active'
        sudo journalctl -u aleph-backend -n 40 --no-pager || true
        exit 1
    fi

    HEALTH=\$(curl -sS --max-time 5 http://127.0.0.1:8765/health || echo 'UNREACHABLE')
    echo \"[deploy] /health -> \$HEALTH\"
"

# ---------------------------------------------------------------------------
echo ""
log "Deploy complete."
echo "  URL (login at /aleph/login.html): https://example.com/aleph/"
echo "  Health (internal, on VM):         sudo -u www-data curl -sS http://127.0.0.1:8765/health"
if [ "$SKIP_APACHE" = false ]; then
    echo "  Login (public):                   curl -X POST -H 'Content-Type: application/json' \\"
    echo "                                      -d '{\"username\":\"${HTPASSWD_USER_VAL}\",\"password\":\"<pw>\"}' \\"
    echo "                                      https://example.com/aleph/api/auth/login"
fi
echo "  Logs:                              gcloud compute ssh $PROD_VM --zone=$PROD_ZONE --project=$PROD_PROJECT --command='journalctl -u aleph-backend -f'"
