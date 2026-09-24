-- 10-pending-events.sql
-- Очередь сообщений для nlp_processor: парсер пишет, процессор читает (SKIP LOCKED).

CREATE TABLE IF NOT EXISTS pending_events (
    id BIGSERIAL PRIMARY KEY,
    message_id BIGINT NOT NULL,
    text TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    photo_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'done', 'error', 'expired')),
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    processed_at TIMESTAMPTZ,
    locked_at TIMESTAMPTZ,
    worker_id TEXT,
    UNIQUE(message_id, event_time)
);

CREATE INDEX IF NOT EXISTS idx_pending_events_status
    ON pending_events(status, created_at)
    WHERE status = 'pending';

-- Поиск зависших задач для фонового очистителя (R-PR11).
CREATE INDEX IF NOT EXISTS idx_pending_events_stale
    ON pending_events(locked_at)
    WHERE status = 'processing';

-- NOTIFY parser о необходимости скачать фото после обработки events.
-- ВАЖНО: event_id берётся из events (резолв по message_id+event_time),
-- а НЕ NEW.id — id в pending_events и events не совпадают (разные sequences),
-- иначе photo_url никогда не привязывается к событию.
CREATE OR REPLACE FUNCTION notify_photo_download()
RETURNS TRIGGER AS $$
DECLARE
    v_event_id BIGINT;
BEGIN
    IF NEW.status = 'done' AND OLD.status IS DISTINCT FROM 'done'
       AND NEW.photo_file_id IS NOT NULL THEN
        SELECT e.id INTO v_event_id
        FROM events e
        WHERE e.message_id = NEW.message_id
          AND e.event_time = NEW.event_time
        LIMIT 1;
        IF v_event_id IS NOT NULL THEN
            PERFORM pg_notify('photo_download',
                jsonb_build_object(
                    'event_id', v_event_id,
                    'message_id', NEW.message_id,
                    'photo_file_id', NEW.photo_file_id
                )::text
            );
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_pending_events_photo_notify
    AFTER UPDATE ON pending_events
    FOR EACH ROW
    EXECUTE FUNCTION notify_photo_download();
