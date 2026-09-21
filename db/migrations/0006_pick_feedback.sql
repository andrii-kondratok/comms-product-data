-- ============================================================================
--  0006 — оцінки топу дня
--  2026-09-21
--
--  Вибір редакції — слабкий сигнал: вона бачить не всі новини, тож «не взяли»
--  часто означає «не бачили». Головний сигнал для налаштування — пряма оцінка
--  топу: добре, погано (і чому), дублікат. Такі мітки важать у навчанні більше
--  за дайджест.
-- ============================================================================

CREATE TABLE feedback.pick_feedback (
    feedback_id   bigserial PRIMARY KEY,
    day           date NOT NULL,
    candidate_id  bigint NOT NULL REFERENCES ops.candidate_pool(candidate_id) ON DELETE CASCADE,
    verdict       text NOT NULL CHECK (verdict IN ('good','ok','bad','duplicate')),
    -- ok        — переглянуто, зауважень немає
    -- duplicate — та сама подія, що й duplicate_of: це вада дедуплікації, не відбору
    duplicate_of  bigint REFERENCES ops.candidate_pool(candidate_id),
    reason        text,
    reviewer      text NOT NULL,
    rank_shown    smallint,
    model_version text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON feedback.pick_feedback (candidate_id);
CREATE INDEX ON feedback.pick_feedback (day, verdict);
