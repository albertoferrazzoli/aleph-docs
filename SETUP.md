# Setup runbook — from zero to running

This document is written to be executed **by an AI coder agent** (or a
human) on a clean Ubuntu 22.04+ / Debian 12+ VM with SSH and sudo. It
brings up the full stack (MCP + Aleph viewer + PostgreSQL + pgvector +
Apache reverse proxy + TLS) in about 30 minutes of wall time.

> Commands assume the agent runs them with `sudo` available. Replace any
> `<PLACEHOLDER>` with a real value. Prompt the user for secrets; never
> commit them.

---

## 0. Prerequisites checklist

- [ ] A Linux VM reachable via SSH (Ubuntu 22.04 / Debian 12 tested).
- [ ] A DNS name pointing at the VM, e.g. `aleph.example.com`.
      (Or access via IP + a self-signed cert.)
- [ ] A **GitHub Personal Access Token** with `repo` scope, restricted
      to the Markdown docs repo you want indexed.
      Generate at: https://github.com/settings/personal-access-tokens/new
- [ ] A **Gemini API key** (Google AI Studio or GCP Generative Language API).
      Generate at: https://aistudio.google.com/app/apikey
- [ ] A **second GitHub PAT** with *write* scope on the docs repo, if you
      want `propose_doc_patch(open_pr=true)` to work. Optional.
- [ ] A random 32-byte hex string for `MCP_API_KEY` and another for
      `ALEPH_API_KEY`. Generate with `openssl rand -hex 32`.
- [ ] A username + password for HTTP Basic Auth on `/aleph`.

Collect all values before proceeding; the setup assumes they exist.

---

## 1. On your laptop: clone + configure

```bash
git clone git@github.com:YOURORG/aleph-docs.git
cd aleph-docs
cp .env.example .env
$EDITOR .env      # fill every value — see comments inside
```

The `.env` at the repo root is read by the two deploy scripts. Keep it
out of git (it is gitignored).

---

## 2. On the VM: base packages

```bash
# System
sudo apt-get update
sudo apt-get install -y curl gnupg ca-certificates apache2 apache2-utils \
    python3 python3-venv python3-pip git

# PostgreSQL 16 + pgvector from PGDG (Ubuntu default ships pg14).
. /etc/os-release
echo "deb https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg
sudo apt-get update
sudo apt-get install -y postgresql-16 postgresql-contrib postgresql-16-pgvector
sudo systemctl enable --now postgresql

# Apache modules
sudo a2enmod proxy proxy_http headers rewrite auth_basic authn_file ssl
sudo systemctl reload apache2

# Node 20 (for the Aleph frontend build — can also be done on your laptop)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

---

## 3. Postgres bootstrap

```bash
sudo -u postgres createdb aleph_memory
sudo -u postgres createuser aleph --login
sudo -u postgres psql -c "ALTER USER aleph WITH PASSWORD '<PG_PASSWORD>'"
sudo -u postgres psql aleph_memory -c "CREATE EXTENSION IF NOT EXISTS vector"
sudo -u postgres psql aleph_memory -c "CREATE EXTENSION IF NOT EXISTS pgcrypto"
sudo -u postgres psql aleph_memory -c "GRANT ALL ON SCHEMA public TO aleph"
sudo -u postgres psql aleph_memory -c \
  "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO aleph"
sudo -u postgres psql aleph_memory -c \
  "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO aleph"
```

Record the PG_PASSWORD in your local `.env`:

```
PG_PASSWORD=<value>
PG_DSN=postgresql://aleph:<value>@localhost:5432/aleph_memory
```

---

## 4. Deploy the MCP server

From your laptop:

```bash
cd aleph-docs
./mcp/deploy-mcp.sh -y
```

The script (idempotent):
1. Reads secrets from `./.env`.
2. Uploads `mcp/` to `/opt/aleph-docs/mcp/` on the VM.
3. Creates a Python venv and installs `requirements.txt`.
4. Applies `mcp/memory/schema.sql`.
5. Installs systemd units:
   - `aleph-docs-mcp.service` — the always-on MCP server.
   - `aleph-docs-mcp-update.timer` — hourly `indexer.py --update`.
   - `aleph-docs-mcp-lint.timer` — every 6h smart lint.
6. Enables + starts everything.
7. Runs `memory.bootstrap` if the memory table is empty.

Verify:

```bash
ssh <VM> 'sudo systemctl status aleph-docs-mcp --no-pager | head'
ssh <VM> 'curl -H "Authorization: Bearer <MCP_API_KEY>" http://127.0.0.1:8001/health'
# → {"status":"ok","pages":N,"memory_enabled":true,"memory_count":N,...}
```

---

## 5. Expose the MCP over HTTPS (Apache)

Decide a public path. Example: `https://aleph.example.com/mcp`.

Add to your Apache vhost (or a new one):

```apache
ProxyPass        /mcp/sse  http://127.0.0.1:8001/sse flushpackets=on timeout=86400
ProxyPassReverse /mcp/sse  http://127.0.0.1:8001/sse
<Location /mcp/sse>
    SetEnv proxy-sendchunked 1
    Header set X-Accel-Buffering "no"
</Location>
ProxyPass        /messages/ http://127.0.0.1:8001/messages/
ProxyPassReverse /messages/ http://127.0.0.1:8001/messages/
ProxyPass        /mcp      http://127.0.0.1:8001/mcp
ProxyPassReverse /mcp      http://127.0.0.1:8001/mcp
```

Reload + test:

```bash
sudo apache2ctl configtest && sudo systemctl reload apache2
curl -H "Authorization: Bearer <MCP_API_KEY>" https://aleph.example.com/mcp/health
```

TLS: use Let's Encrypt (`certbot --apache`) or whatever your org uses.

---

## 6. Deploy the Aleph viewer

The Aleph viewer reuses the MCP's memory package and database — no
migration needed.

```bash
cd aleph-docs
./aleph/deploy-aleph.sh -y
```

The script:
1. Builds the frontend (`npm ci && npm run build` on your laptop).
2. Uploads `aleph/backend/` + `frontend/dist/` to `/opt/aleph-docs/aleph/`.
3. Applies `aleph/backend/schema_additions.sql` + `triggers.sql`.
4. Installs `aleph-backend.service` (uvicorn on 127.0.0.1:8765) and
   `aleph-projection.timer` (hourly UMAP rebuild).
5. Adds a marker block to Apache that serves `/aleph/` and proxies
   `/aleph/api/*` with Basic Auth. Backs up the config first and
   rolls back on `configtest` failure.
6. Creates `/etc/apache2/aleph.htpasswd` with your `HTPASSWD_USER` /
   `HTPASSWD_PASSWORD`.
7. Runs `projection.main()` to produce the first snapshot.

Verify:

```bash
curl -u <HTPASSWD_USER>:<HTPASSWD_PASSWORD> https://aleph.example.com/aleph/api/health
# → {"status":"ok","memory_count":N,"snapshot_version":1,"graph_nodes":N}
open https://aleph.example.com/aleph/
# Login with the Basic Auth creds; the 3D viewer loads.
```

---

## 7. Connect Claude Desktop (optional)

Once the MCP answers on `https://aleph.example.com/mcp`:

```bash
# On your laptop
mkdir -p "$HOME/Library/Application Support/Claude"
cat >> "$HOME/Library/Application Support/Claude/claude_desktop_config.json" <<'JSON'
{
  "mcpServers": {
    "aleph-docs": {
      "type": "url",
      "url": "https://aleph.example.com/mcp",
      "headers": { "Authorization": "Bearer <MCP_API_KEY>" }
    }
  }
}
JSON
# Restart Claude Desktop.
```

Then in Claude Desktop create a Project and paste the contents of
[`mcp/PROJECT_INSTRUCTIONS.md`](mcp/PROJECT_INSTRUCTIONS.md) as the
system prompt. It teaches Claude when to use which tool.

---

## 8. Smoke test end-to-end

From Claude Desktop (after the project is configured):

1. "Usando il tool `semantic_search`, cerca X in documentazione."
   → Should return hits grounded in your docs.
2. "Salva via `remember` questo insight: 'Y'."
   → New pink node appears in the Aleph 3D viewer within ~1 s.
3. "Chiama `lint_run(mode='cheap')`."
   → Returns a summary; no-op if memory is empty.

If any step fails, check logs:

```bash
ssh <VM> 'journalctl -u aleph-docs-mcp -n 50 --no-pager'
ssh <VM> 'journalctl -u aleph-backend -n 50 --no-pager'
ssh <VM> 'journalctl -u aleph-projection -n 50 --no-pager'
```

---

## 9. Day-2 operations

- **New docs** — push to the docs repo; the hourly `indexer.py --update`
  timer picks them up. Manual refresh: `sudo systemctl start aleph-docs-mcp-update`.
- **Bootstrap cost** — first full embedding of a few thousand chunks is
  around $0.10–$0.50 with Gemini. Incremental updates are cents per week.
- **Lint cost** — the LLM contradiction judge runs at most weekly with a
  cap of 20 pairs per run → negligible (~$0.06/year at defaults).
- **Backups** — `pg_dump aleph_memory | zstd > backup-$(date +%F).sql.zst`.
  The docs are always re-derivable from the source git repo, but
  insights + interactions are NOT — back up the DB.
- **Renew TLS** — whatever you use for other Apache vhosts; the Aleph
  location inherits the same TLS stack.

---

## 10. Uninstall / re-bootstrap

```bash
# On the VM
sudo systemctl stop aleph-docs-mcp aleph-backend
sudo systemctl disable aleph-docs-mcp aleph-docs-mcp-update.timer \
    aleph-docs-mcp-lint.timer aleph-backend aleph-projection.timer
sudo rm -rf /opt/aleph-docs
sudo rm /etc/systemd/system/aleph-docs-mcp* /etc/systemd/system/aleph-backend* \
        /etc/systemd/system/aleph-projection*
sudo systemctl daemon-reload
# Remove the Apache marker block (between `# --- ALEPH BEGIN ---` and END)
sudo $EDITOR /etc/apache2/sites-enabled/*.conf
sudo systemctl reload apache2
# Drop DB
sudo -u postgres dropdb aleph_memory
sudo -u postgres dropuser aleph
```

The repo itself stays intact; you can redeploy by re-running the scripts.
