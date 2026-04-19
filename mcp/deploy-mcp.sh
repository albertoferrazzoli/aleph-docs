#!/bin/bash

# Deploy the Aleph Docs MCP server to a Linux VM via SSH (example: GCP).
# Adapt PROD_VM / PROD_ZONE / PROD_PROJECT to your environment,
# or replace gcloud compute ssh/scp with plain ssh/scp.
# Usage: ./deploy-mcp.sh [-y] [--skip-pg] [--help]

set -euo pipefail

PROD_VM="<VM_NAME>"
PROD_ZONE="europe-west1-d"
PROD_PROJECT="<GCP_PROJECT>"
MCP_REMOTE_PATH="/opt/aleph-docs/mcp"
MCP_LOCAL_PATH="$(cd "$(dirname "$0")" && pwd)/mcp"
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="aleph-docs-mcp"
UPDATER_NAME="aleph-docs-mcp-update"
LINT_NAME="aleph-docs-mcp-lint"
PG_DB_NAME="aleph_memory"
PG_ROLE_NAME="aleph"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()   { echo -e "${GREEN}[MCP]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

SKIP_CONFIRM=false
SKIP_PG=false
for arg in "$@"; do
    case "$arg" in
        -y|--yes) SKIP_CONFIRM=true ;;
        --skip-pg) SKIP_PG=true ;;
        -h|--help)
            cat <<EOF
Usage: $(basename "$0") [options]

Options:
  -y, --yes        Skip the interactive confirmation prompt.
  --skip-pg        Skip Postgres install / DB provisioning / schema apply /
                   bootstrap step (fast code-only re-deploys).
  -h, --help       Show this help and exit.
EOF
            exit 0
            ;;
    esac
done

[ -f "$MCP_LOCAL_PATH/server.py" ] || error "server.py not found in $MCP_LOCAL_PATH"
[ -f "$MCP_LOCAL_PATH/requirements.txt" ] || error "requirements.txt not found"

echo ""
echo "=================================================="
echo "  DEPLOY MCP Aleph Docs Server"
echo "=================================================="
echo "  From: $MCP_LOCAL_PATH"
echo "  To:   $PROD_VM:$MCP_REMOTE_PATH"
echo "  Port: 8001 (localhost)"
echo "=================================================="
echo ""

if [ "$SKIP_CONFIRM" = false ]; then
    read -p "Deploy? (y/n): " response
    [ "$response" = "y" ] || [ "$response" = "yes" ] || { echo "Aborted."; exit 0; }
fi

echo "--- [1/7] Reading secrets from local .env"
ENV_FILE="$DEPLOY_DIR/.env"
_read_env_var() {
    # $1 = key; prints the stripped value from $ENV_FILE (no echo of the key itself)
    [ -f "$ENV_FILE" ] || return 0
    grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}
GOOGLE_API_KEY_VAL=$(_read_env_var GOOGLE_API_KEY)
if [ -z "${GOOGLE_API_KEY_VAL:-}" ]; then
    log "WARN: GOOGLE_API_KEY missing in $ENV_FILE — semantic memory embeddings will fail on the VM until set."
else
    log "GOOGLE_API_KEY loaded (not shown)."
fi
DOCS_WRITE_TOKEN_VAL=$(_read_env_var DOCS_WRITE_TOKEN)
if [ -n "${DOCS_WRITE_TOKEN_VAL:-}" ]; then
    log "DOCS_WRITE_TOKEN loaded (not shown)."
fi

if [ "$SKIP_PG" = false ]; then
    if [ ! -f "$ENV_FILE" ]; then
        error "Local .env not found at $ENV_FILE (needed for PG_PASSWORD). Use --skip-pg to bypass."
    fi
    PG_PASSWORD=$(_read_env_var PG_PASSWORD)
    if [ -z "${PG_PASSWORD:-}" ]; then
        error "PG_PASSWORD missing in $ENV_FILE (or empty). Use --skip-pg to bypass."
    fi
    log "PG_PASSWORD loaded (not shown)."
else
    log "--skip-pg: skipping Postgres install / schema apply / bootstrap."
    PG_PASSWORD=""
fi

echo "--- [2/7] Creating archive"
log "Creating archive..."
TMPFILE=$(mktemp /tmp/aleph-docs-mcp-XXXXXX.tar.gz)
COPYFILE_DISABLE=1 tar -czf "$TMPFILE" \
    --exclude='.env' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.venv' \
    --exclude='data' \
    --exclude='repo' \
    -C "$(dirname "$MCP_LOCAL_PATH")" "$(basename "$MCP_LOCAL_PATH")"

echo "--- [3/7] Uploading archive to production"
log "Uploading to production..."
gcloud compute scp "$TMPFILE" "$PROD_VM:/tmp/aleph-docs-mcp.tar.gz" \
    --zone="$PROD_ZONE" --project="$PROD_PROJECT"
rm -f "$TMPFILE"

echo "--- [4/7] Installing code + systemd units on production"
log "Installing on production..."
gcloud compute ssh "$PROD_VM" --zone="$PROD_ZONE" --project="$PROD_PROJECT" --command="
    set -e
    sudo mkdir -p $MCP_REMOTE_PATH

    # Preserve .env and data/ if they exist
    if [ -f $MCP_REMOTE_PATH/.env ]; then
        sudo cp $MCP_REMOTE_PATH/.env /tmp/mcp-bd-env-backup
    fi
    if [ -d $MCP_REMOTE_PATH/data ]; then
        sudo cp -r $MCP_REMOTE_PATH/data /tmp/mcp-bd-data-backup
    fi

    sudo tar -xzf /tmp/aleph-docs-mcp.tar.gz -C $(dirname $MCP_REMOTE_PATH)
    rm -f /tmp/aleph-docs-mcp.tar.gz

    if [ -f /tmp/mcp-bd-env-backup ]; then
        sudo mv /tmp/mcp-bd-env-backup $MCP_REMOTE_PATH/.env
    fi
    sudo mkdir -p $MCP_REMOTE_PATH/data
    if [ -d /tmp/mcp-bd-data-backup ]; then
        sudo cp -a /tmp/mcp-bd-data-backup/. $MCP_REMOTE_PATH/data/
        sudo rm -rf /tmp/mcp-bd-data-backup
    fi

    # Virtualenv + deps
    if [ ! -d $MCP_REMOTE_PATH/.venv ]; then
        sudo python3 -m venv $MCP_REMOTE_PATH/.venv
    fi
    sudo $MCP_REMOTE_PATH/.venv/bin/pip install -r $MCP_REMOTE_PATH/requirements.txt --quiet

    sudo chown -R www-data:www-data $MCP_REMOTE_PATH

    # Main service
    sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null << 'UNIT'
[Unit]
Description=MCP Aleph Docs Server
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=$MCP_REMOTE_PATH
EnvironmentFile=$MCP_REMOTE_PATH/.env
ExecStart=$MCP_REMOTE_PATH/.venv/bin/python $MCP_REMOTE_PATH/server.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

    # Hourly updater (oneshot + timer)
    sudo tee /etc/systemd/system/$UPDATER_NAME.service > /dev/null << 'UNIT'
[Unit]
Description=Update Aleph Docs index from GitHub

[Service]
Type=oneshot
User=www-data
WorkingDirectory=$MCP_REMOTE_PATH
EnvironmentFile=$MCP_REMOTE_PATH/.env
ExecStart=$MCP_REMOTE_PATH/.venv/bin/python $MCP_REMOTE_PATH/indexer.py --update
UNIT

    sudo tee /etc/systemd/system/$UPDATER_NAME.timer > /dev/null << 'UNIT'
[Unit]
Description=Run Aleph Docs updater hourly

[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
Unit=$UPDATER_NAME.service

[Install]
WantedBy=timers.target
UNIT

    # WP content updater (oneshot + timer)
[Unit]
Description=Update Aleph Docs WP content index from example.com

[Service]
Type=oneshot
User=www-data
WorkingDirectory=$MCP_REMOTE_PATH
EnvironmentFile=$MCP_REMOTE_PATH/.env
UNIT

[Unit]
Description=Run Aleph Docs WP content updater every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
UNIT

    # Memory lint (oneshot + timer, runs every 6h, cost-capped)
    sudo tee /etc/systemd/system/$LINT_NAME.service > /dev/null << 'UNIT'
[Unit]
Description=MCP Aleph Docs memory lint (oneshot)
After=network.target

[Service]
Type=oneshot
User=www-data
WorkingDirectory=$MCP_REMOTE_PATH
EnvironmentFile=$MCP_REMOTE_PATH/.env
ExecStart=$MCP_REMOTE_PATH/.venv/bin/python -m memory.lint_cli --mode auto
UNIT

    sudo tee /etc/systemd/system/$LINT_NAME.timer > /dev/null << 'UNIT'
[Unit]
Description=Run Aleph Docs memory lint every 6h

[Timer]
OnBootSec=15min
OnUnitActiveSec=6h
Persistent=true
Unit=$LINT_NAME.service

[Install]
WantedBy=timers.target
UNIT

    sudo systemctl daemon-reload
    sudo systemctl enable $SERVICE_NAME
    sudo systemctl enable $UPDATER_NAME.timer
    sudo systemctl enable $LINT_NAME.timer
"

if [ "$SKIP_PG" = false ]; then
    echo "--- [5/7] Postgres 16 + pgvector install / DB provisioning / schema apply"
    log "Provisioning Postgres on production..."
    # Pass PG_PASSWORD via env to avoid it appearing on the remote process list
    # in ps output for long. It still crosses the SSH channel (encrypted) and
    # is materialized inside psql for CREATE ROLE only.
    gcloud compute ssh "$PROD_VM" --zone="$PROD_ZONE" --project="$PROD_PROJECT" --command="
        set -e
        export PG_PASSWORD='$PG_PASSWORD'

        # 1a. Recover from any interrupted dpkg run (non-interactive)
        sudo DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true dpkg --configure -a >/dev/null 2>&1 || true

        # 1b. Add PGDG apt repo (jammy default has only pg14; pg16+pgvector come from pgdg)
        if [ ! -f /etc/apt/sources.list.d/pgdg.list ]; then
            . /etc/os-release
            echo \"deb https://apt.postgresql.org/pub/repos/apt \$VERSION_CODENAME-pgdg main\" \\
                | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
            curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \\
                | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg
        fi

        # 1c. Install Postgres 16 + pgvector (idempotent)
        if ! dpkg -s postgresql-16 >/dev/null 2>&1 || \\
           ! dpkg -s postgresql-16-pgvector >/dev/null 2>&1; then
            sudo apt-get update
            sudo DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true \\
                apt-get install -y postgresql-16 postgresql-contrib postgresql-16-pgvector
        fi
        sudo systemctl enable --now postgresql

        # 1d. Install ffmpeg (required by the video/audio chunkers when using
        # a multimodal embed backend; no-op if not using them).
        if ! command -v ffmpeg >/dev/null 2>&1; then
            sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ffmpeg
        fi

        # 2. Create DB + role (idempotent)
        sudo -u postgres psql -tc \"SELECT 1 FROM pg_database WHERE datname='$PG_DB_NAME'\" | grep -q 1 \\
            || sudo -u postgres createdb $PG_DB_NAME

        if ! sudo -u postgres psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$PG_ROLE_NAME'\" | grep -q 1; then
            # Pipe SQL via stdin (password on stdin, not argv).
            printf \"CREATE ROLE $PG_ROLE_NAME LOGIN PASSWORD %s;\\n\" \\
                \"'\$(printf '%s' \"\$PG_PASSWORD\" | sed \"s/'/''/g\")'\" \\
                | sudo -u postgres psql -v ON_ERROR_STOP=1 >/dev/null
        fi

        # Always re-assert extensions + grants (idempotent)
        sudo -u postgres psql $PG_DB_NAME -c 'CREATE EXTENSION IF NOT EXISTS vector' >/dev/null
        sudo -u postgres psql $PG_DB_NAME -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto' >/dev/null
        sudo -u postgres psql $PG_DB_NAME -c 'GRANT ALL ON SCHEMA public TO $PG_ROLE_NAME' >/dev/null
        sudo -u postgres psql $PG_DB_NAME -c 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $PG_ROLE_NAME' >/dev/null

        # 3. Apply schema.sql (uploaded with the rest of the source)
        if [ -f $MCP_REMOTE_PATH/memory/schema.sql ]; then
            sudo -u postgres psql -v ON_ERROR_STOP=1 $PG_DB_NAME \\
                -f $MCP_REMOTE_PATH/memory/schema.sql >/dev/null
        else
            echo '[deploy] WARNING: $MCP_REMOTE_PATH/memory/schema.sql not found — skipping schema apply'
        fi

        unset PG_PASSWORD
    "
else
    echo "--- [5/7] Postgres provisioning (SKIPPED: --skip-pg)"
fi

echo "--- [6/7] Upsert PG_DSN / MEMORY_ENABLED in remote .env + restart service"
log "Updating remote .env and restarting service..."
if [ "$SKIP_PG" = false ]; then
    PG_DSN_VALUE="postgresql://$PG_ROLE_NAME:$PG_PASSWORD@localhost:5432/$PG_DB_NAME"
else
    PG_DSN_VALUE=""
fi

gcloud compute ssh "$PROD_VM" --zone="$PROD_ZONE" --project="$PROD_PROJECT" --command="
    set -e
    ENVF=$MCP_REMOTE_PATH/.env
    if [ ! -f \"\$ENVF\" ]; then
        sudo touch \"\$ENVF\"
        sudo chown www-data:www-data \"\$ENVF\"
        sudo chmod 600 \"\$ENVF\"
    fi

    upsert_env() {
        local key=\"\$1\" val=\"\$2\"
        if sudo grep -qE \"^\${key}=\" \"\$ENVF\"; then
            # Use a tmp file; sed -i with sensitive values risks partial writes.
            sudo sed -i.bak \"s|^\${key}=.*|\${key}=\${val}|\" \"\$ENVF\"
            sudo rm -f \"\${ENVF}.bak\"
        else
            echo \"\${key}=\${val}\" | sudo tee -a \"\$ENVF\" >/dev/null
        fi
    }

    if [ -n '$PG_DSN_VALUE' ]; then
        upsert_env PG_DSN '$PG_DSN_VALUE'
        upsert_env MEMORY_ENABLED 'true'
    fi
    if [ -n '$GOOGLE_API_KEY_VAL' ]; then
        upsert_env GOOGLE_API_KEY '$GOOGLE_API_KEY_VAL'
        upsert_env EMBED_MODEL 'gemini-embedding-001'
        upsert_env EMBED_DIM '1536'
    fi
    if [ -n '$DOCS_WRITE_TOKEN_VAL' ]; then
        upsert_env DOCS_WRITE_TOKEN '$DOCS_WRITE_TOKEN_VAL'
    fi
    sudo chown www-data:www-data \"\$ENVF\"
    sudo chmod 600 \"\$ENVF\"

    sudo systemctl restart $SERVICE_NAME
    sudo systemctl start $UPDATER_NAME.timer
    sudo systemctl start $LINT_NAME.timer

    sleep 2
    sudo systemctl is-active $SERVICE_NAME && echo 'Service running' || echo 'WARNING: service failed'
"

if [ "$SKIP_PG" = false ]; then
    echo "--- [7/7] Bootstrap semantic memory (if DB empty)"
    log "Checking if memories table is empty and running bootstrap if so..."
    gcloud compute ssh "$PROD_VM" --zone="$PROD_ZONE" --project="$PROD_PROJECT" --command="
        set -e
        EMPTY=\$(sudo -u postgres psql -tAc \"SELECT COUNT(*)=0 FROM memories\" $PG_DB_NAME 2>/dev/null | head -1 || echo 'unknown')
        if [ \"\$EMPTY\" = 't' ]; then
            echo '[deploy] memories empty — running bootstrap (this may take a while)'
            cd $MCP_REMOTE_PATH && \\
              sudo -u www-data env -C $MCP_REMOTE_PATH \\
                $MCP_REMOTE_PATH/.venv/bin/python -m memory.bootstrap \\
              || echo '[deploy] bootstrap failed (non-fatal — you can re-run manually)'
        else
            echo \"[deploy] memories already populated (empty=\$EMPTY) — skipping bootstrap\"
        fi
    "
else
    echo "--- [7/7] Semantic memory bootstrap (SKIPPED: --skip-pg)"
fi

log "Deploy complete!"

# Check .env exists on remote
ENV_EXISTS=$(gcloud compute ssh "$PROD_VM" --zone="$PROD_ZONE" --project="$PROD_PROJECT" \
    --command="[ -f $MCP_REMOTE_PATH/.env ] && echo yes || echo no" 2>/dev/null)

if [ "$ENV_EXISTS" = "no" ]; then
    warn "No .env file on production! Create it with:"
    echo ""
    echo "  gcloud compute ssh $PROD_VM --zone=$PROD_ZONE --project=$PROD_PROJECT --command=\"sudo tee $MCP_REMOTE_PATH/.env << EOF"
    echo "MCP_API_KEY=\$(openssl rand -hex 32)"
    echo "MCP_HOST=127.0.0.1"
    echo "MCP_PORT=8001"
    echo "DOCS_REPO_URL=https://github.com/<DOCS_REPO_SLUG>.git"
    echo "DOCS_REPO_BRANCH=main"
    echo "DOCS_REPO_PATH=$MCP_REMOTE_PATH/repo"
    echo "DOCS_DB_PATH=$MCP_REMOTE_PATH/data/index.db"
    echo "WP_DB_HOST=127.0.0.1"
    echo "WP_DB_PORT=3306"
    echo "WP_DB_USER=alephdocs_ro"
    echo "WP_DB_PASS=<ro-password>"
    echo "WP_DB_NAME=alephdb"
    echo "WP_TABLE_PREFIX=wp_"
    echo "WP_SITE_URL=https://example.com"
    echo "EOF\""
    echo ""
    warn "Then restart: sudo systemctl restart $SERVICE_NAME"
fi

echo ""
log "Next steps:"
echo "  1. Ensure .env exists with MCP_API_KEY set"
echo "  2. Add Apache proxy config in wordpress.conf:"
echo "       ProxyPass        /mcp/sse  http://localhost:8001/sse"
echo "       ProxyPassReverse /mcp/sse  http://localhost:8001/sse"
echo "       ProxyPass        /mcp      http://localhost:8001/mcp"
echo "       ProxyPassReverse /mcp      http://localhost:8001/mcp"
echo "     Then: sudo systemctl reload apache2"
echo "  3. Test:"
echo "       curl -H 'Authorization: Bearer <KEY>' https://example.com/mcp"
