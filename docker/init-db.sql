-- Run automatically by the pgvector/pgvector:pg16 image on first startup
-- for the POSTGRES_DB defined in the compose file. Enables the extensions
-- the MCP and Aleph backend rely on and grants the aleph role access to
-- any sequences created later.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aleph;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO aleph;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO aleph;
