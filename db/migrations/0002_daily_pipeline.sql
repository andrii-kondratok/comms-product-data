-- ============================================================================
--  0002 — щоденний конвеєр
--  2026-09-18
--
--  Що змінюється і навіщо:
--   1. Задачі за розкладом стають сутностями бази: ops.job + ops.run.job.
--      Видно, що коли бігло, скільки взяло і де впало.
--   2. Інкрементальність через водяні знаки (ops.watermark): кожна задача
--      бере лише нове з моменту останнього успішного прогону.
--   3. Пул кандидатів — один рядок на URL, а не на кожне бачення. Щоденний збір
--      бачить ту саму статтю по кілька разів; дублікати зіпсували б негативи.
--   4. Дотягування тексту — черга з повторами і журналом спроб. Спосіб
--      дотягування задається в реєстрі джерел, а не в коді.
--   5. Облік API-викликів: тріали й тарифи рахують запити, ми теж маємо.
--   6. Notion читається інкрементально за last_edited_time.
-- ============================================================================


-- ---------------------------------------------------------------- 1. задачі

CREATE TABLE ops.job (
    job               text PRIMARY KEY,           -- 'discover_rss'
    description       text NOT NULL,
    schedule          text NOT NULL,              -- cron, час Києва: '*/15 * * * *'
    enabled           boolean NOT NULL DEFAULT true,
    timeout_minutes   integer NOT NULL DEFAULT 30,
    last_run_id       uuid,
    last_status       text,
    last_started_at   timestamptz,
    last_finished_at  timestamptz,
    consecutive_failures integer NOT NULL DEFAULT 0
);

INSERT INTO ops.job (job, description, schedule, timeout_minutes) VALUES
  ('discover_rss',          'RSS і news-sitemap усіх джерел → пул кандидатів',            '5 * * * *',       20),
  ('discover_newscatcher',  'Тематичні запити NewsCatcher за 24 год → пул кандидатів',   '0 7,13,19 * * *', 15),
  ('fetch_articles',        'Текст статей для свіжих кандидатів: каскад із реєстру',     '*/30 * * * *',    25),
  ('sync_notion',           'Articles, Content Pulse, Post Metrics → core, інкрементально', '*/15 * * * *', 14),
  ('enrich_post_metrics',   'Post text і Source у Post Metrics зі сторінок Content Pulse', '7,22,37,52 * * * *', 14),
  ('link_posts',            'Пари стаття → пост із явних посилань',                      '40 * * * *',      20),
  ('reconcile_candidates',  'Кандидат, що потрапив у дайджест, → outcome = ingested',     '50 * * * *',      10),
  ('backup',                'pg_dump у том бекапів, 14 днів',                             '30 3 * * *',      60);

ALTER TABLE ops.run
    ADD COLUMN job   text REFERENCES ops.job(job),
    ADD COLUMN stats jsonb NOT NULL DEFAULT '{}'::jsonb;   -- що зроблено: {"seen": 812, "new": 64}

CREATE INDEX ON ops.run (job, started_at DESC);


-- ------------------------------------------------------- 2. водяні знаки

-- Остання точка, до якої задача все обробила. Оновлюється лише після
-- успішного завершення — інакше впалий прогін загубив би вікно.
CREATE TABLE ops.watermark (
    job         text NOT NULL REFERENCES ops.job(job),
    key         text NOT NULL,                    -- 'post_metrics.last_edited_time'
    value       text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job, key)
);


-- ------------------------------------------------------- 3. реєстр джерел

ALTER TABLE core.source DROP CONSTRAINT source_access_class_check;
ALTER TABLE core.source ADD CONSTRAINT source_access_class_check
    CHECK (access_class IN ('open','metered','antibot','hard_paywall','unknown'));

ALTER TABLE core.source
    -- як дізнаємось, що вийшло
    ADD COLUMN sitemap_url      text,
    ADD COLUMN discovery        text[] NOT NULL DEFAULT '{}',   -- {'rss','sitemap','newscatcher'}
    -- як дістаємо текст: у цьому порядку, до першого успіху
    ADD COLUMN fetch_cascade    text[] NOT NULL DEFAULT '{own_extractor}',
    -- виміряна придатність власного витягу
    ADD COLUMN extract_verdict  text,
    ADD COLUMN extract_ratio    numeric(5,2),
    ADD COLUMN access_src       text CHECK (access_src IN ('measured','assumed')),
    -- API віддає лише початок статті — такий текст у навчання не йде
    ADD COLUMN api_preview_only boolean NOT NULL DEFAULT false,
    ADD COLUMN updated_at       timestamptz NOT NULL DEFAULT now();


-- ------------------------------------------------------- 4. пул кандидатів

-- Було: рядок на кожне бачення. Стало: рядок на URL + лічильник.
ALTER TABLE ops.candidate_pool
    ADD COLUMN first_seen_at timestamptz,
    ADD COLUMN last_seen_at  timestamptz,
    ADD COLUMN seen_count    integer NOT NULL DEFAULT 1,
    ADD COLUMN providers     text[] NOT NULL DEFAULT '{}',   -- {'rss','newscatcher'}
    ADD COLUMN topics        text[] NOT NULL DEFAULT '{}',   -- тематичні блоки запиту
    ADD COLUMN lang          text,
    ADD COLUMN description   text,
    -- кластер провайдера: скільки видань написали про ту саму подію
    ADD COLUMN provider_cluster_id text,
    ADD COLUMN cluster_size  integer,
    -- адреса як прийшла: канонікалізація знімає www., а NewsCatcher search_by_link
    -- шукає точний збіг і без www. статтю не знаходить
    ADD COLUMN url_original  text;

UPDATE ops.candidate_pool SET first_seen_at = seen_at, last_seen_at = seen_at
 WHERE first_seen_at IS NULL;
ALTER TABLE ops.candidate_pool
    ALTER COLUMN first_seen_at SET NOT NULL,
    ALTER COLUMN first_seen_at SET DEFAULT now(),
    ALTER COLUMN last_seen_at  SET NOT NULL,
    ALTER COLUMN last_seen_at  SET DEFAULT now();

CREATE UNIQUE INDEX candidate_pool_url_uq ON ops.candidate_pool (url_canonical);
CREATE INDEX ON ops.candidate_pool (published_at DESC);
CREATE INDEX ON ops.candidate_pool (article_id) WHERE article_id IS NULL;


-- ------------------------------------------------- 5. стаття і дотягування

ALTER TABLE core.article DROP CONSTRAINT article_retrieval_status_check;
ALTER TABLE core.article ADD CONSTRAINT article_retrieval_status_check
    CHECK (retrieval_status IN
           ('pending','full_text','paywall_stub','too_short','blocked','not_found','error'));

ALTER TABLE core.article
    -- звідки текст: це різні ліцензійні й якісні класи, змішувати не можна
    ADD COLUMN retrieval_method text CHECK (retrieval_method IN
               ('notion_editor','own_extractor','newscatcher_v3','provider_other','manual')),
    ADD COLUMN retrieval_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN next_retry_at    timestamptz,
    ADD COLUMN last_error       text,
    -- текст звірено з повною копією або домен перевірено контролем
    ADD COLUMN text_verified    boolean,
    ADD COLUMN description      text,
    ADD COLUMN url_original     text,
    ADD COLUMN notion_page_id   text,             -- сторінка в 🧾 Articles, якщо є
    ADD COLUMN digest_status    text,             -- статус у Notion як є
    ADD COLUMN updated_at       timestamptz NOT NULL DEFAULT now();

CREATE UNIQUE INDEX article_notion_page_uq ON core.article (notion_page_id)
    WHERE notion_page_id IS NOT NULL;
CREATE INDEX article_retry_idx ON core.article (next_retry_at)
    WHERE retrieval_status IN ('pending','error','blocked');

-- Журнал спроб. Append-only: видно, що саме пробували і чим закінчилось.
CREATE TABLE ops.fetch_attempt (
    attempt_id    bigserial PRIMARY KEY,
    article_id    uuid NOT NULL REFERENCES core.article(article_id) ON DELETE CASCADE,
    run_id        uuid REFERENCES ops.run(run_id),
    method        text NOT NULL,
    http_status   text,
    chars         integer,
    verdict       text NOT NULL,                  -- full | teaser | blocked_or_empty | over_extracted | error
    error         text,
    attempted_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON ops.fetch_attempt (article_id, attempted_at DESC);
CREATE INDEX ON ops.fetch_attempt (method, verdict, attempted_at DESC);


-- ------------------------------------------------------- 6. облік API

CREATE TABLE ops.api_call (
    call_id          bigserial PRIMARY KEY,
    provider         text NOT NULL,               -- 'newscatcher_v3'
    endpoint         text NOT NULL,               -- '/api/search'
    run_id           uuid REFERENCES ops.run(run_id),
    request          jsonb,                       -- тіло запиту БЕЗ ключа
    http_status      text,
    items_requested  integer,
    items_returned   integer,
    latency_ms       integer,
    error            text,
    called_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON ops.api_call (provider, called_at DESC);


-- ------------------------------------------------------- 7. Notion

ALTER TABLE raw.notion_export
    ADD COLUMN last_edited_time timestamptz,
    ADD COLUMN payload_hash     text;

-- Той самий стан сторінки двічі не пишемо
CREATE UNIQUE INDEX notion_export_state_uq
    ON raw.notion_export (datasource, notion_page_id, payload_hash);


-- ------------------------------------------------------- 8. пости

ALTER TABLE core.post
    ADD COLUMN text_source            text,        -- 'notion_post_metrics' | 'x_archive' | ...
    ADD COLUMN source_url             text,        -- Source / Evidence (regex) як є
    ADD COLUMN content_pulse_page_id  text,
    ADD COLUMN notion_last_edited_at  timestamptz,
    ADD COLUMN updated_at             timestamptz NOT NULL DEFAULT now();

CREATE UNIQUE INDEX post_notion_page_uq ON core.post (notion_page_id)
    WHERE notion_page_id IS NOT NULL;


-- ------------------------------------------------------- 9. зв'язок стаття → пост

ALTER TABLE core.article_post_link
    ADD COLUMN link_evidence text CHECK (link_evidence IN
               ('source_field','url_in_post','notion_relation','pipeline','model')),
    ADD COLUMN quality_flag  text;                -- post_fragment | article_too_long | ...


-- ------------------------------------------------------- 10. вітрини

-- Пари для навчання: лише явні зв'язки з повним текстом обох сторін.
CREATE VIEW marts.article_post_pairs AS
SELECT l.article_id, l.post_id, l.link_method, l.link_evidence, l.quality_flag,
       p.platform, p.posted_at, p.text_source,
       coalesce(p.body_raw, p.hook_raw) AS post_text,
       a.url_canonical, a.title, a.retrieval_method, a.text_verified,
       a.body_text AS article_text
FROM core.article_post_link l
JOIN core.post p    USING (post_id)
JOIN core.article a USING (article_id)
WHERE l.link_method = 'explicit_url'
  AND a.retrieval_status = 'full_text'
  AND length(coalesce(p.body_raw, p.hook_raw, '')) >= 80;

-- Стан задач: для дашборда й для того, хто чергує.
CREATE VIEW marts.pipeline_health AS
SELECT j.job, j.enabled, j.schedule, j.last_status,
       j.last_started_at, j.last_finished_at, j.consecutive_failures,
       now() - j.last_finished_at AS since_last_finish,
       r.stats, r.error
FROM ops.job j
LEFT JOIN ops.run r ON r.run_id = j.last_run_id
ORDER BY j.job;

-- Щоденний приплив по джерелах.
CREATE VIEW marts.daily_intake AS
SELECT date_trunc('day', c.first_seen_at AT TIME ZONE 'Europe/Kyiv')::date AS day,
       s.domain,
       count(*)                                         AS candidates,
       count(*) FILTER (WHERE a.retrieval_status = 'full_text') AS with_text,
       count(*) FILTER (WHERE c.outcome = 'ingested')   AS in_digest
FROM ops.candidate_pool c
LEFT JOIN core.source s  USING (source_id)
LEFT JOIN core.article a ON a.article_id = c.article_id
GROUP BY 1, 2;

-- Витрати API по днях.
CREATE VIEW marts.api_usage_daily AS
SELECT date_trunc('day', called_at AT TIME ZONE 'Europe/Kyiv')::date AS day,
       provider, endpoint,
       count(*) AS calls,
       sum(items_returned) AS items,
       count(*) FILTER (WHERE http_status NOT IN ('200')) AS failed,
       round(avg(latency_ms)) AS avg_latency_ms
FROM ops.api_call
GROUP BY 1, 2, 3;
