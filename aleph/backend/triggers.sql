-- Aleph NOTIFY triggers on `memories`. Idempotent.
-- Fires a `memory_change` NOTIFY on every row-level change so the SSE stream
-- in the Aleph backend can fan-out deltas to connected viewers.

CREATE OR REPLACE FUNCTION memory_change_notify() RETURNS trigger AS $$
DECLARE
    payload JSON;
    row_id  UUID;
    row_kind TEXT;
    row_path TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        row_id := OLD.id;
        row_kind := OLD.kind::text;
        row_path := OLD.source_path;
    ELSE
        row_id := NEW.id;
        row_kind := NEW.kind::text;
        row_path := NEW.source_path;
    END IF;

    payload := json_build_object(
        'op',          lower(TG_OP),
        'id',          row_id,
        'kind',        row_kind,
        'source_path', row_path
    );

    PERFORM pg_notify('memory_change', payload::text);
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memory_change_trg ON memories;
CREATE TRIGGER memory_change_trg
    AFTER INSERT OR UPDATE OR DELETE ON memories
    FOR EACH ROW EXECUTE FUNCTION memory_change_notify();
