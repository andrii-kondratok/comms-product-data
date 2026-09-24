-- ============================================================================
--  0008 — «ми про це вже писали»
--  2026-09-24
--
--  Дві різні перевірки, які раніше плутались:
--    * дедуплікація між статтями дня — одна подія, кілька видань (вже була);
--    * покриття: стаття про те, про що ми вже опублікували пост, або про що
--      стаття вже лежить у дайджесті. Це не дубль у видачі, а «не потрібно».
--
--  Поріг калібрували на наших парах «стаття → пост» (posts_db/eval_coverage.py).
--  Результат: розділення немає — схожість статті з ЇЇ постом (медіана 0.65) така сама,
--  як із постом того ж тижня про іншу подію (0.62). Тому covered_vector лишається
--  вимкненим, а covered_*_min нижче — лише запобіжник, якщо його колись увімкнуть.
--  Подробиці: docs/selection/05_coverage_eval.md
-- ============================================================================

-- Вікно свіжості — доба: новина, старша за добу, для коментаря вже не годиться.
-- Працює лише перевірка за посиланням (точна); за схожістю — вимкнена, див. вище.
UPDATE ml.ranker_model
   SET params = params || '{"fresh_hours": 24, "covered_vector": false,
                            "covered_post_days": 30, "covered_digest_days": 7,
                            "covered_post_min": 0.80, "covered_digest_min": 0.80}'::jsonb
 WHERE version = 'transparent-v1';

CREATE TABLE ml.candidate_coverage (
    candidate_id  bigint NOT NULL REFERENCES ops.candidate_pool(candidate_id) ON DELETE CASCADE,
    kind          text   NOT NULL CHECK (kind IN ('post', 'digest')),
    model         text   NOT NULL,
    score         real   NOT NULL,           -- 1.0 для збігу за посиланням, інакше схожість
    ref_post_id   uuid REFERENCES core.post(post_id) ON DELETE SET NULL,
    ref_article_id uuid REFERENCES core.article(article_id) ON DELETE SET NULL,
    checked_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (candidate_id, kind)
);

CREATE INDEX ON ml.candidate_coverage (kind, score DESC);

-- Ембединги постів рахуються з тексту поста (перші 700 знаків треду).
-- Таблиця core.post_embedding є з 0001, але досі стояла порожня.
COMMENT ON TABLE core.post_embedding IS
    'Вектор тексту поста (перші 700 знаків). Використовується для перевірки, чи ми вже писали про цю подію.';

DROP VIEW marts.daily_top;
CREATE VIEW marts.daily_top AS
SELECT p.day, p.rank, p.score, p.topic_code, t.name_uk AS topic, p.event_size,
       c.title, coalesce(c.url_original, c.url_canonical) AS url, s.domain,
       c.published_at, c.outcome, p.model_version, p.computed_at,
       cov.score AS covered_post_score, cov.ref_post_id,
       (SELECT left(coalesce(po.body_raw, po.hook_raw), 120) FROM core.post po
        WHERE po.post_id = cov.ref_post_id) AS covered_post_text
FROM ml.daily_pick p
JOIN ops.candidate_pool c USING (candidate_id)
LEFT JOIN core.source s ON s.source_id = c.source_id
LEFT JOIN core.topic t ON t.topic_code = p.topic_code
LEFT JOIN ml.candidate_coverage cov ON cov.candidate_id = p.candidate_id AND cov.kind = 'post'
WHERE p.computed_at = (SELECT max(computed_at) FROM ml.daily_pick x WHERE x.day = p.day)
ORDER BY p.day DESC, p.rank;

-- Що відсіяно як «вже писали»: видно, чи фільтр не зайвий
CREATE VIEW marts.covered_today AS
SELECT (c.first_seen_at AT TIME ZONE 'Europe/Kyiv')::date AS day,
       c.title, coalesce(c.url_original, c.url_canonical) AS url, s.domain,
       cov.kind, cov.score,
       left(coalesce(po.body_raw, po.hook_raw), 160) AS our_post,
       po.posted_at, ar.title AS digest_article
FROM ml.candidate_coverage cov
JOIN ops.candidate_pool c USING (candidate_id)
LEFT JOIN core.source s ON s.source_id = c.source_id
LEFT JOIN core.post po ON po.post_id = cov.ref_post_id
LEFT JOIN core.article ar ON ar.article_id = cov.ref_article_id
WHERE c.first_seen_at >= now() - interval '3 days'
ORDER BY cov.score DESC;
