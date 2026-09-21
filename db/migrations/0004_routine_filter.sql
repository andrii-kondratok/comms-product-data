-- ============================================================================
--  0004 — фільтр рутини, вищий поріг, версія таксономії
--  2026-09-21
--
--  Рутина — новини, що повторюються щодня й самі не дають приводу для коментаря:
--  «нічний обстріл, N загиблих», тривоги, ДТП. Описуються антитемами (routine_facet).
--  Стаття відкидається, якщо вона помітно ближча до рутини, ніж до своєї теми:
--  рутина ≥ тема + margin. Запас потрібен: без нього відкидались історії людей і
--  аналітика про втрати, які схожі на рутину словами, але дали пости.
--
--  Версія таксономії: коли теми чи антитеми змінюються, статті переоцінюються.
--  Без неї старі бали лишались би поруч із новими правилами.
-- ============================================================================

CREATE TABLE core.routine_facet (
    facet_id    bigserial PRIMARY KEY,
    text        text NOT NULL,
    model       text NOT NULL,
    embedding   vector(1024) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (text, model)
);

ALTER TABLE ml.article_topic ADD COLUMN taxonomy_version text NOT NULL DEFAULT 'v0';
ALTER TABLE ml.article_topic DROP CONSTRAINT article_topic_pkey;
ALTER TABLE ml.article_topic ADD PRIMARY KEY (article_id, topic_code, model, taxonomy_version);

-- Схожість із рутиною — одна на статтю й версію
CREATE TABLE ml.article_routine (
    article_id       uuid NOT NULL REFERENCES core.article(article_id) ON DELETE CASCADE,
    model            text NOT NULL,
    taxonomy_version text NOT NULL,
    score            real NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, model, taxonomy_version)
);

ALTER TABLE ml.topic_threshold
    ADD COLUMN routine_margin   real NOT NULL DEFAULT 0.08,
    ADD COLUMN taxonomy_version text;

-- posts_db/eval_threshold.py (2026-09-21): поріг 0.47 + рутина ≥ тема + 0.08.
-- Проходить 87% статей, що дали пост, і 28% кандидатів, яких редакція не взяла
-- (було 95% / 43% за порогу 0.443 без фільтра). Запас 0.10 пропускав зведення, що
-- відрізнялось на 0.098; на еталоні постів 0.08 і 0.10 однакові.
UPDATE ml.topic_threshold
   SET threshold = 0.47, routine_margin = 0.08, updated_at = now(),
       calibrated_on = 'eval_threshold.py 2026-09-21: пости 87% / не взяті 28%, рутина +0.08'
 WHERE model = 'BAAI/bge-m3';

DROP VIEW marts.topic_queue;
CREATE VIEW marts.topic_queue AS
SELECT a.article_id, a.title, a.url_canonical, s.domain,
       coalesce(a.published_at, a.ingested_at) AS published_at,
       t.topic_code, tp.name_uk AS topic, t.score, r.score AS routine_score,
       (r.score >= t.score + th.routine_margin) AS is_routine,
       (t.score >= th.threshold AND NOT coalesce(r.score >= t.score + th.routine_margin, false))
           AS above_threshold,
       ARRAY(SELECT g.goal_code FROM core.topic_goal g WHERE g.topic_code = t.topic_code) AS goals,
       c.cluster_size, a.retrieval_status
FROM ml.article_topic t
JOIN ml.topic_threshold th ON th.model = t.model AND th.taxonomy_version = t.taxonomy_version
JOIN core.article a USING (article_id)
JOIN core.topic tp USING (topic_code)
LEFT JOIN ml.article_routine r ON r.article_id = t.article_id AND r.model = t.model
                              AND r.taxonomy_version = t.taxonomy_version
LEFT JOIN core.source s USING (source_id)
LEFT JOIN ops.candidate_pool c ON c.article_id = a.article_id
WHERE t.rank = 1
  AND coalesce(a.published_at, a.ingested_at) >= now() - interval '3 days';
