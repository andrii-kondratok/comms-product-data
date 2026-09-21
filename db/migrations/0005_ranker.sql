-- ============================================================================
--  0005 — реранкер і топ-20 дня
--  2026-09-21
--
--  Ранжування вчиться на виборі редакції: кандидат, що потрапив у дайджест
--  (ops.candidate_pool.outcome = 'ingested'), — позитив; решта того ж дня — негатив.
--  Сутність — кандидат, а не стаття: негативи живуть саме в пулі.
--
--  Перевірка posts_db/eval_ranker.py (2026-09-21, 3 дні, 101 взята стаття,
--  навчання на двох днях → перевірка на третьому): AUC 0.92 проти 0.81 за балом
--  теми; взятих у топ-20 — 17% проти 6%. Головна ознака — схожість на статті,
--  з яких ТМ робив пости.
-- ============================================================================

-- Ембединги кандидатів: текст — заголовок і опис, як у score_topics
CREATE TABLE ml.candidate_embedding (
    candidate_id  bigint PRIMARY KEY REFERENCES ops.candidate_pool(candidate_id) ON DELETE CASCADE,
    model         text NOT NULL,
    embedding     vector(1024) NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Версія реранкера: параметри, на чому навчено, як перевірено
CREATE TABLE ml.ranker_model (
    version          text PRIMARY KEY,           -- 'lr-20260921-1530'
    trained_at       timestamptz NOT NULL DEFAULT now(),
    embed_model      text NOT NULL,
    taxonomy_version text NOT NULL,
    features         text[] NOT NULL,
    params           jsonb NOT NULL,             -- коефіцієнти, середні, масштаби
    train_days       date[] NOT NULL,
    holdout_day      date,
    metrics          jsonb NOT NULL DEFAULT '{}',-- на відкладеному дні: auc, взятих у топ-20
    is_active        boolean NOT NULL DEFAULT false
);

CREATE UNIQUE INDEX ranker_one_active ON ml.ranker_model (is_active) WHERE is_active;

-- Топ дня. Перераховується щогодини; кожен перерахунок — новий зріз (computed_at),
-- щоб було видно, як змінювався список упродовж дня.
CREATE TABLE ml.daily_pick (
    day            date NOT NULL,
    candidate_id   bigint NOT NULL REFERENCES ops.candidate_pool(candidate_id) ON DELETE CASCADE,
    rank           smallint NOT NULL,
    score          real NOT NULL,
    topic_code     text REFERENCES core.topic(topic_code),
    event_size     smallint,                    -- скільки статей дня злилось у цей пункт (схожість ≥ 0.70)
    model_version  text NOT NULL REFERENCES ml.ranker_model(version),
    computed_at    timestamptz NOT NULL,
    PRIMARY KEY (day, candidate_id, computed_at)
);

CREATE INDEX ON ml.daily_pick (day, computed_at DESC, rank);

INSERT INTO ops.job (job, description, schedule, timeout_minutes) VALUES
  ('train_ranker', 'Перенавчання реранкера на розмічених днях пулу', '0 4 * * 1', 40),
  ('rank_daily',   'Топ-20 дня: ранжування кандидатів і дедуплікація подій', '55 * * * *', 20);

-- Останній зріз топу за кожен день
CREATE VIEW marts.daily_top AS
SELECT p.day, p.rank, p.score, p.topic_code, t.name_uk AS topic, p.event_size,
       c.title, coalesce(c.url_original, c.url_canonical) AS url, s.domain,
       c.published_at, c.outcome, p.model_version, p.computed_at
FROM ml.daily_pick p
JOIN ops.candidate_pool c USING (candidate_id)
LEFT JOIN core.source s ON s.source_id = c.source_id
LEFT JOIN core.topic t ON t.topic_code = p.topic_code
WHERE p.computed_at = (SELECT max(computed_at) FROM ml.daily_pick x WHERE x.day = p.day)
ORDER BY p.day DESC, p.rank;
