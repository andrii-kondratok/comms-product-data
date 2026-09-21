-- ============================================================================
--  0003 — тематичний відбір статей
--  2026-09-21
--
--  Теми й цілі — з Notion «Operational programming + strategic objectives».
--  Операційна тема = що ми пишемо; стратегічна ціль = навіщо. Зв'язок
--  «багато-до-багатьох», тому окрема таблиця topic_goal.
--
--  Відбір: ембединг статті порівнюється з фасетами тем (pgvector, косинус);
--  схожість із темою = максимум по її фасетах. Результат — окремі рядки
--  ml.article_topic на кожну версію моделі, без перезапису.
-- ============================================================================

CREATE TABLE core.strategic_goal (
    goal_code   text PRIMARY KEY,
    name_uk     text NOT NULL,
    pm_labels   text[] NOT NULL DEFAULT '{}'   -- як ця ціль підписана в Post Metrics.Strategic Goal
);

CREATE TABLE core.topic (
    topic_code  text PRIMARY KEY,
    topic_no    smallint NOT NULL UNIQUE,      -- номер у документі редакції
    block       text NOT NULL,
    name_uk     text NOT NULL,
    from_news   boolean NOT NULL,              -- false: тема народжується в офісі, не в новинах
    is_active   boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.topic_goal (
    topic_code  text NOT NULL REFERENCES core.topic(topic_code) ON DELETE CASCADE,
    goal_code   text NOT NULL REFERENCES core.strategic_goal(goal_code) ON DELETE CASCADE,
    PRIMARY KEY (topic_code, goal_code)
);

-- Кілька формулювань на тему: широка тема має кілька облич
CREATE TABLE core.topic_facet (
    facet_id    bigserial PRIMARY KEY,
    topic_code  text NOT NULL REFERENCES core.topic(topic_code) ON DELETE CASCADE,
    text        text NOT NULL,
    model       text NOT NULL,
    embedding   vector(1024) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (topic_code, text, model)
);

-- Схожість статті з темою. Кожна модель — свої рядки, старі не перезаписуються.
CREATE TABLE ml.article_topic (
    article_id  uuid NOT NULL REFERENCES core.article(article_id) ON DELETE CASCADE,
    topic_code  text NOT NULL REFERENCES core.topic(topic_code) ON DELETE CASCADE,
    model       text NOT NULL,
    score       real NOT NULL,                 -- косинус з найближчим фасетом
    rank        smallint NOT NULL,             -- 1 = найближча тема статті
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, topic_code, model)
);

CREATE INDEX ON ml.article_topic (topic_code, score DESC);
CREATE INDEX ON ml.article_topic (article_id) WHERE rank = 1;

-- Поріг «цікаво» тримаємо в базі: його калібрують на даних, а не в коді
CREATE TABLE ml.topic_threshold (
    model       text PRIMARY KEY,
    threshold   real NOT NULL,
    calibrated_on text,                        -- що саме лягло в калібрування
    updated_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO ops.job (job, description, schedule, timeout_minutes) VALUES
  ('score_topics', 'Ембединги свіжих статей і схожість із темами редакції', '15,45 * * * *', 25);

-- Черга для редактора: свіжі статті, найближча тема, цілі, на які вона працює
CREATE VIEW marts.topic_queue AS
SELECT a.article_id, a.title, a.url_canonical, s.domain,
       coalesce(a.published_at, a.ingested_at) AS published_at,
       t.topic_code, tp.name_uk AS topic, t.score,
       (t.score >= th.threshold) AS above_threshold,
       ARRAY(SELECT g.goal_code FROM core.topic_goal g WHERE g.topic_code = t.topic_code) AS goals,
       c.cluster_size, a.retrieval_status
FROM ml.article_topic t
JOIN core.article a USING (article_id)
JOIN core.topic tp USING (topic_code)
LEFT JOIN core.source s USING (source_id)
LEFT JOIN ml.topic_threshold th ON th.model = t.model
LEFT JOIN ops.candidate_pool c ON c.article_id = a.article_id
WHERE t.rank = 1
  AND coalesce(a.published_at, a.ingested_at) >= now() - interval '3 days';

-- Поріг відкалібровано posts_db/eval_topics.py (2026-09-21): проходить 90% статей
-- дайджесту і 44% кандидатів, яких редакція не взяла. AUC 0.816 за заголовками.
INSERT INTO ml.topic_threshold (model, threshold, calibrated_on) VALUES
  ('BAAI/bge-m3', 0.443, 'eval_topics.py 2026-09-21: дайджест 2 727 vs пул 3 008, recall 0.90');
