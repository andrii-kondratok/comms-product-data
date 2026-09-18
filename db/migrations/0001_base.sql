-- ============================================================================
--  Comms Product — схема Postgres для бекенду
--  Автор: Андрій (Data Architect) · 2026-09-12
--
--  Принципи:
--   1. Артефакти моделі незмінні. Новий прогін = новий рядок, не UPDATE.
--      Інакше неможливо порівняти дві версії моделі на тій самій статті.
--   2. Рішення людини — append-only лог подій, а не поле «статус».
--   3. Кожен артефакт знає, якою версією моделі й промпта породжений.
--   4. Notion — проєкція. Джерело правди тут. Зв'язок через ops.notion_sync.
--   5. Сирі відповіді провайдера зберігаються як прийшли, окремо від розібраних.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;     -- ембединги для дедуплікації
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- нечіткий пошук по тексту

CREATE SCHEMA IF NOT EXISTS raw;       -- як прийшло ззовні
CREATE SCHEMA IF NOT EXISTS core;      -- канонічні сутності
CREATE SCHEMA IF NOT EXISTS ml;        -- артефакти моделі
CREATE SCHEMA IF NOT EXISTS feedback;  -- рішення людей
CREATE SCHEMA IF NOT EXISTS ops;       -- прогони, версії, синхронізація
CREATE SCHEMA IF NOT EXISTS marts;     -- вітрини для дашбордів і навчання


-- ============================================================================
--  OPS — версії, прогони, синхронізація
--  Цей блок іде першим, бо на нього посилається майже все інше.
-- ============================================================================

-- Версія моделі. Без неї метрики якості не з чим порівнювати.
CREATE TABLE ops.model_version (
    model_version_id  bigserial PRIMARY KEY,
    name              text NOT NULL UNIQUE,       -- 'qwen2.5-14b-tm-style-v3'
    stage             text NOT NULL CHECK (stage IN ('relevance','extraction','draft','embedding')),
    base_model        text,                       -- 'Qwen/Qwen2.5-14B-Instruct'
    adapter_ref       text,                       -- шлях/hash LoRA-адаптера
    training_corpus   text,                       -- що саме пішло в навчання
    trained_at        timestamptz,
    is_active         boolean NOT NULL DEFAULT false,
    notes             text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- Промпт версіонується окремо від моделі: та сама модель з іншим промптом
-- дає інший результат, і це треба вміти розрізняти.
CREATE TABLE ops.prompt_version (
    prompt_version_id bigserial PRIMARY KEY,
    stage             text NOT NULL CHECK (stage IN ('relevance','extraction','draft')),
    template          text NOT NULL,
    template_hash     text NOT NULL,
    is_active         boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stage, template_hash)
);

-- Один запуск конвеєра: кнопка в Notion, розклад або ручний бекфіл.
CREATE TABLE ops.run (
    run_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger           text NOT NULL CHECK (trigger IN ('notion_button','schedule','backfill','manual')),
    triggered_by      text,                       -- користувач Notion або 'cron'
    notion_page_id    text,                       -- рядок, з якого натиснули
    pipeline_version  text NOT NULL,              -- git sha бекенду
    -- Захист від подвійного натискання: те саме створює той самий прогін.
    idempotency_key   text UNIQUE,
    status            text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running','done','failed','cancelled')),
    articles_seen     integer DEFAULT 0,
    drafts_created    integer DEFAULT 0,
    cost_usd          numeric(10,4) DEFAULT 0,
    error             text,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);

CREATE INDEX ON ops.run (status, started_at DESC);

-- Дзеркало Notion. Дає ідемпотентність: повторна публікація не створює дубль.
CREATE TABLE ops.notion_sync (
    entity_type       text NOT NULL CHECK (entity_type IN ('article','draft','post','review')),
    entity_id         text NOT NULL,
    notion_page_id    text NOT NULL,
    notion_datasource text NOT NULL,              -- collection://...
    last_pushed_at    timestamptz,
    last_payload_hash text,                       -- щоб не писати незмінене
    sync_status       text NOT NULL DEFAULT 'ok'
                      CHECK (sync_status IN ('ok','pending','failed')),
    error             text,
    PRIMARY KEY (entity_type, entity_id)
);

CREATE UNIQUE INDEX ON ops.notion_sync (notion_page_id);


-- ============================================================================
--  RAW — те, що прийшло ззовні, без інтерпретації
-- ============================================================================

CREATE TABLE raw.article_payload (
    payload_id    bigserial PRIMARY KEY,
    provider      text NOT NULL,                  -- 'newscatcher' | 'perigon' | 'rss' | ...
    provider_id   text,                           -- id статті у провайдера
    requested_url text,
    http_status   integer,
    payload       jsonb NOT NULL,
    content_hash  text,
    run_id        uuid REFERENCES ops.run(run_id),
    fetched_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON raw.article_payload (provider, fetched_at DESC);
CREATE INDEX ON raw.article_payload USING gin (payload jsonb_path_ops);

-- Знімок вивантаження з Notion. Потрібен, бо Notion міняють руками:
-- без знімка неможливо довести, що саме ми читали на момент завантаження.
CREATE TABLE raw.notion_export (
    export_id     bigserial PRIMARY KEY,
    datasource    text NOT NULL,
    notion_page_id text NOT NULL,
    properties    jsonb NOT NULL,
    page_body     text,
    exported_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON raw.notion_export (datasource, exported_at DESC);


-- ============================================================================
--  CORE — канонічні сутності
-- ============================================================================

-- Реєстр джерел. Наповнюється з posts_db/sources.csv.
CREATE TABLE core.source (
    source_id      bigserial PRIMARY KEY,
    domain         text NOT NULL UNIQUE,
    name           text NOT NULL,
    category       text CHECK (category IN ('global','ua_media','ru_media','analysis','kse','other')),
    lang           text,
    country        text,
    -- Чи можна взагалі дістати текст
    access_class   text NOT NULL DEFAULT 'unknown'
                   CHECK (access_class IN ('open','metered','hard_paywall','unknown')),
    -- Що нам дозволено з тим текстом робити
    license_class  text NOT NULL DEFAULT 'unknown'
                   CHECK (license_class IN ('rss_public','scraped','licensed_full_text','unknown')),
    rss_url        text,
    declared_in_digest boolean DEFAULT false,      -- чи є в списку Digest Layout
    is_active      boolean NOT NULL DEFAULT true,
    notes          text
);

-- Група статей про одну подію в різних медіа.
CREATE TABLE core.article_cluster (
    cluster_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    method         text NOT NULL,                 -- 'minhash' | 'embedding' | 'provider'
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.article (
    article_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id      bigint REFERENCES core.source(source_id),
    cluster_id     uuid REFERENCES core.article_cluster(cluster_id),

    url_canonical  text NOT NULL UNIQUE,          -- після зняття utm/amp/трекерів
    content_hash   text,                          -- sha256 нормалізованого тіла
    title          text,
    subtitle       text,
    body_text      text,                          -- ВНУТРІШНЄ. Ніколи не публікується.
    author         text,
    published_at   timestamptz,
    lang           text,
    word_count     integer GENERATED ALWAYS AS
                   (CASE WHEN body_text IS NULL THEN NULL
                         ELSE array_length(regexp_split_to_array(btrim(body_text), '\s+'), 1)
                    END) STORED,

    -- Чому тексту може не бути — розрізняємо «не пробували» і «пейвол»
    retrieval_status text NOT NULL DEFAULT 'pending'
                     CHECK (retrieval_status IN
                            ('pending','full_text','paywall_stub','too_short','not_found','error')),
    paywalled      boolean,

    raw_payload_id bigint REFERENCES raw.article_payload(payload_id),
    first_seen_run uuid REFERENCES ops.run(run_id),
    ingested_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON core.article (published_at DESC);
CREATE INDEX ON core.article (source_id, published_at DESC);
CREATE INDEX ON core.article (retrieval_status) WHERE retrieval_status <> 'full_text';
CREATE UNIQUE INDEX ON core.article (content_hash) WHERE content_hash IS NOT NULL;

CREATE TABLE core.article_embedding (
    article_id  uuid PRIMARY KEY REFERENCES core.article(article_id) ON DELETE CASCADE,
    model       text NOT NULL,
    embedding   vector(1024) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON core.article_embedding USING hnsw (embedding vector_cosine_ops);


-- ---------------------------------------------------------------- пости
-- Опубліковані пости. Вивантажуються з Notion + локальних експортів.
CREATE TABLE core.post (
    post_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    platform       text NOT NULL CHECK (platform IN ('X','FB','TG','Threads','Inst','LinkedIn')),
    native_id      text,                          -- status id / story_fbid
    url_canonical  text,

    posted_at      timestamptz,
    lang           text,

    -- Текст у трьох станах — рішення з posts_db, переноситься сюди як є
    hook_raw       text,        -- перший твіт / весь допис FB, як у джерелі
    hook_clean     text,        -- без маркерів нумерації — це йде у fine-tune
    body_raw       text,        -- повний тред
    numbering_style text CHECK (numbering_style IN ('leading','trailing','none')),
    style_epoch    text,        -- запобіжник: у навчання йде лише дозволений набір
    is_thread      boolean,
    thread_len     integer,
    char_count     integer GENERATED ALWAYS AS (length(coalesce(body_raw, hook_raw, ''))) STORED,

    -- Редакційна категорія контенту. Джерело записується окремо, бо розмітки
    -- з Notion і зі скрейпера збігаються лише на 71% — це не один довідник.
    content_category     text,
    content_category_src text CHECK (content_category_src IN
                         ('notion_post_metrics', 'notion_content_pulse', 'scraper')),

    -- Чи придатний для навчання. Рішення явне, а не виведене на льоту.
    training_eligible boolean NOT NULL DEFAULT false,
    exclusion_reason  text,

    source_system  text NOT NULL,                 -- 'notion_content_pulse' | 'x_hooks_csv' | ...
    notion_page_id text,
    ingested_at    timestamptz NOT NULL DEFAULT now(),

    UNIQUE (platform, native_id)
);

CREATE INDEX ON core.post (posted_at DESC);
CREATE INDEX ON core.post (platform, posted_at DESC);
CREATE INDEX ON core.post (training_eligible) WHERE training_eligible;
CREATE INDEX ON core.post USING gin (hook_clean gin_trgm_ops);

-- Метрики — часовий ряд, а не колонки. Перегляди ростуть тижнями,
-- і «скільки було через 72 години» та «скільки зараз» — різні питання.
CREATE TABLE core.post_metric (
    post_id      uuid NOT NULL REFERENCES core.post(post_id) ON DELETE CASCADE,
    metric       text NOT NULL CHECK (metric IN
                 ('impressions','views','likes','comments','shares','reach','saves')),
    value        double precision NOT NULL,
    observed_at  timestamptz NOT NULL,
    is_frozen    boolean NOT NULL DEFAULT false,  -- зафіксовано на 72 год
    source_system text NOT NULL,
    PRIMARY KEY (post_id, metric, observed_at, source_system)
);

CREATE INDEX ON core.post_metric (metric, observed_at DESC);

CREATE TABLE core.post_embedding (
    post_id     uuid PRIMARY KEY REFERENCES core.post(post_id) ON DELETE CASCADE,
    model       text NOT NULL,
    embedding   vector(1024) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON core.post_embedding USING hnsw (embedding vector_cosine_ops);

-- Зв'язок «стаття → пост». Окрема таблиця, бо частина зв'язків імовірнісна.
CREATE TABLE core.article_post_link (
    article_id   uuid NOT NULL REFERENCES core.article(article_id) ON DELETE CASCADE,
    post_id      uuid NOT NULL REFERENCES core.post(post_id) ON DELETE CASCADE,
    link_method  text NOT NULL CHECK (link_method IN
                 ('explicit_url','pipeline','temporal_semantic','manual')),
    confidence   numeric(3,2) CHECK (confidence BETWEEN 0 AND 1),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, post_id)
);

CREATE INDEX ON core.article_post_link (link_method, confidence DESC);


-- ============================================================================
--  OPS — пул кандидатів
--  Найважливіша таблиця, яку найлегше забути. Без неї немає негативів.
-- ============================================================================

CREATE TABLE ops.candidate_pool (
    candidate_id   bigserial PRIMARY KEY,
    run_id         uuid REFERENCES ops.run(run_id),
    url_canonical  text NOT NULL,
    source_id      bigint REFERENCES core.source(source_id),
    title          text,
    published_at   timestamptz,
    provider       text,
    -- Ключове поле: чи пройшла стаття далі, і якщо ні — чому
    outcome        text NOT NULL DEFAULT 'seen'
                   CHECK (outcome IN ('seen','ingested','filtered_rule','filtered_model','duplicate')),
    filter_reason  text,
    article_id     uuid REFERENCES core.article(article_id),
    seen_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON ops.candidate_pool (seen_at DESC);
CREATE INDEX ON ops.candidate_pool (outcome, seen_at DESC);
CREATE INDEX ON ops.candidate_pool (url_canonical);


-- ============================================================================
--  ML — артефакти моделі. Нічого не перезаписується.
-- ============================================================================

CREATE TABLE ml.relevance_verdict (
    verdict_id        bigserial PRIMARY KEY,
    article_id        uuid NOT NULL REFERENCES core.article(article_id) ON DELETE CASCADE,
    run_id            uuid REFERENCES ops.run(run_id),
    model_version_id  bigint REFERENCES ops.model_version(model_version_id),
    prompt_version_id bigint REFERENCES ops.prompt_version(prompt_version_id),

    label             boolean NOT NULL,
    score             numeric(4,3) CHECK (score BETWEEN 0 AND 1),
    tier              smallint CHECK (tier IN (0,1,2)),  -- 0 не взяли / 1 дайджест / 2 пост
    reason            text NOT NULL,                     -- одне речення, вимога one-pager'а

    latency_ms        integer,
    cost_usd          numeric(10,6),
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON ml.relevance_verdict (article_id, created_at DESC);
CREATE INDEX ON ml.relevance_verdict (model_version_id, created_at DESC);

CREATE TABLE ml.extraction (
    extraction_id     bigserial PRIMARY KEY,
    article_id        uuid NOT NULL REFERENCES core.article(article_id) ON DELETE CASCADE,
    run_id            uuid REFERENCES ops.run(run_id),
    model_version_id  bigint REFERENCES ops.model_version(model_version_id),
    prompt_version_id bigint REFERENCES ops.prompt_version(prompt_version_id),

    claim             text NOT NULL,
    angle             text,
    examples          jsonb,
    -- Частка фактів, знайдених у тілі статті дослівно. Рахується автоматично.
    grounding_score   numeric(4,3),
    latency_ms        integer,
    cost_usd          numeric(10,6),
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON ml.extraction (article_id, created_at DESC);

-- Факти окремими рядками, а не в jsonb: інакше грунтування не перевірити запитом.
CREATE TABLE ml.extraction_fact (
    fact_id        bigserial PRIMARY KEY,
    extraction_id  bigint NOT NULL REFERENCES ml.extraction(extraction_id) ON DELETE CASCADE,
    kind           text NOT NULL CHECK (kind IN ('fact','number','quote','example')),
    value_text     text NOT NULL,
    -- Дослівна цитата з оригіналу + її позиція. Це і є перевірка на галюцинацію.
    verbatim_quote text,
    span_start     integer,
    span_end       integer,
    is_grounded    boolean,        -- виставляє валідатор: чи quote є підрядком body_text
    ord            smallint
);

CREATE INDEX ON ml.extraction_fact (extraction_id);
CREATE INDEX ON ml.extraction_fact (is_grounded) WHERE is_grounded IS NOT TRUE;

CREATE TABLE ml.draft (
    draft_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_id     bigint REFERENCES ml.extraction(extraction_id),
    article_id        uuid NOT NULL REFERENCES core.article(article_id),
    run_id            uuid REFERENCES ops.run(run_id),
    model_version_id  bigint REFERENCES ops.model_version(model_version_id),
    prompt_version_id bigint REFERENCES ops.prompt_version(prompt_version_id),

    channel           text NOT NULL CHECK (channel IN ('X','FB','UA X','TG','Threads','LinkedIn')),
    lang              text NOT NULL,
    text              text NOT NULL,
    char_count        integer GENERATED ALWAYS AS (length(text)) STORED,

    -- ── Контроль якості: фіксовані ворота, які має пройти кожен драфт ──
    grounding_score   numeric(4,3),   -- успадковується з extraction
    style_score       numeric(4,3),   -- схожість на референсний корпус
    near_dup_score    numeric(4,3),   -- максимальна схожість на останні N постів
    near_dup_post_id  uuid REFERENCES core.post(post_id),
    length_ok         boolean,
    has_source_link   boolean,
    quote_words_max   integer,        -- найдовша дослівна цитата, для копірайту
    -- Підсумкові ворота: чи можна показувати людині
    qc_status         text NOT NULL DEFAULT 'pending'
                      CHECK (qc_status IN ('pending','passed','flagged','blocked')),
    qc_blocked_reason text,

    -- Версія драфта для тієї самої статті: повторне натискання кнопки
    attempt_no        smallint NOT NULL DEFAULT 1,
    superseded_by     uuid REFERENCES ml.draft(draft_id),

    latency_ms        integer,
    cost_usd          numeric(10,6),
    notion_page_id    text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON ml.draft (article_id, created_at DESC);
CREATE INDEX ON ml.draft (qc_status, created_at DESC);
CREATE INDEX ON ml.draft (run_id);

-- Розширювані перевірки понад фіксовані ворота вище.
-- Нове правило = новий рядок, без міграції схеми.
CREATE TABLE ml.draft_check (
    draft_id    uuid NOT NULL REFERENCES ml.draft(draft_id) ON DELETE CASCADE,
    rule_name   text NOT NULL,           -- 'no_banned_phrase' | 'no_added_numbers' | ...
    passed      boolean NOT NULL,
    severity    text NOT NULL DEFAULT 'warn' CHECK (severity IN ('info','warn','block')),
    detail      text,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (draft_id, rule_name)
);

CREATE INDEX ON ml.draft_check (rule_name, passed);


-- ============================================================================
--  FEEDBACK — рішення людей. Найцінніше, що накопичує система.
-- ============================================================================

-- Довідник причин. Окремою таблицею, щоб редакція могла доповнювати
-- без міграції, а звіти не розсипались на вільному тексті.
CREATE TABLE feedback.reason_code (
    code        text PRIMARY KEY,
    group_name  text NOT NULL CHECK (group_name IN ('relevance','extraction','style','other')),
    label_uk    text NOT NULL,
    description text,
    is_active   boolean NOT NULL DEFAULT true
);

INSERT INTO feedback.reason_code (code, group_name, label_uk) VALUES
    ('not_relevant_topic', 'relevance',  'Не наша тема'),
    ('already_covered',    'relevance',  'Ми це вже писали'),
    ('too_old',            'relevance',  'Застаріла новина'),
    ('not_his_angle',      'relevance',  'Не той кут, який узяв би ТМ'),
    ('source_untrusted',   'relevance',  'Ненадійне джерело'),
    ('fact_distorted',     'extraction', 'Факт спотворено'),
    ('fact_added',         'extraction', 'Додано факт, якого немає в статті'),
    ('number_wrong',       'extraction', 'Помилка в цифрі'),
    ('missed_the_point',   'extraction', 'Пропущено головне'),
    ('quote_wrong',        'extraction', 'Цитата неточна'),
    ('wrong_voice',        'style',      'Не його голос'),
    ('too_long',           'style',      'Задовгий'),
    ('wrong_format',       'style',      'Не той формат'),
    ('cliche',             'style',      'Кліше'),
    ('weak_hook',          'style',      'Слабкий хук'),
    ('duplicate',          'other',      'Дублікат'),
    ('legal_risk',         'other',      'Юридичний ризик'),
    ('other',              'other',      'Інше');

-- Append-only. Жодних UPDATE: історія рішень і є цінністю.
CREATE TABLE feedback.review_event (
    event_id     bigserial PRIMARY KEY,
    draft_id     uuid NOT NULL REFERENCES ml.draft(draft_id) ON DELETE CASCADE,
    reviewer     text NOT NULL,
    action       text NOT NULL CHECK (action IN ('approve','edit','reject','defer')),
    reason_code  text REFERENCES feedback.reason_code(code),
    -- Текст після правок редактора. Пара (draft.text, edited_text) —
    -- паливо для preference-тюнінгу, дорожче за весь корпус постів.
    edited_text  text,
    comment      text,
    notion_page_id text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON feedback.review_event (draft_id, created_at);
CREATE INDEX ON feedback.review_event (action, created_at DESC);
CREATE INDEX ON feedback.review_event (reason_code) WHERE reason_code IS NOT NULL;

-- Еталонні мітки для вимірювання. У навчання не йдуть.
CREATE TABLE feedback.gold_label (
    article_id   uuid NOT NULL REFERENCES core.article(article_id) ON DELETE CASCADE,
    labeler      text NOT NULL,
    tier         smallint NOT NULL CHECK (tier IN (0,1,2)),
    round        smallint NOT NULL DEFAULT 1,
    notes        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, labeler, round)
);


-- ============================================================================
--  ML — вимірювання
-- ============================================================================

CREATE TABLE ml.eval_run (
    eval_run_id       bigserial PRIMARY KEY,
    dataset_version   text NOT NULL,
    stage             text NOT NULL CHECK (stage IN ('relevance','extraction','draft')),
    model_version_id  bigint REFERENCES ops.model_version(model_version_id),
    prompt_version_id bigint REFERENCES ops.prompt_version(prompt_version_id),
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ml.eval_metric (
    eval_run_id  bigint NOT NULL REFERENCES ml.eval_run(eval_run_id) ON DELETE CASCADE,
    metric       text NOT NULL,       -- 'agreement' | 'kappa' | 'grounding_rate' | 'light_edit_rate'
    value        double precision NOT NULL,
    n            integer,
    PRIMARY KEY (eval_run_id, metric)
);


-- ============================================================================
--  MARTS — вітрини
-- ============================================================================

-- Що саме дозволено подавати у fine-tune. Один запит замість домовленостей.
CREATE VIEW marts.style_corpus AS
SELECT p.post_id, p.platform, p.posted_at, p.style_epoch,
       coalesce(p.body_raw, p.hook_clean) AS text,
       m.value AS impressions
FROM core.post p
LEFT JOIN LATERAL (
    SELECT value FROM core.post_metric
    WHERE post_id = p.post_id AND metric IN ('impressions','views')
    ORDER BY observed_at DESC LIMIT 1
) m ON true
WHERE p.training_eligible
  AND length(coalesce(p.body_raw, p.hook_clean, '')) >= 80;

-- Пари «драфт → що редактор реально опублікував».
CREATE VIEW marts.training_pair AS
SELECT d.draft_id, d.article_id, d.channel, d.text AS draft_text,
       r.edited_text AS final_text, r.action, r.reason_code,
       d.model_version_id, d.prompt_version_id, r.created_at
FROM ml.draft d
JOIN feedback.review_event r USING (draft_id)
WHERE r.action IN ('edit','approve')
  AND r.edited_text IS NOT NULL;

-- Останній вердикт на статтю — для черги редактора.
CREATE VIEW marts.article_queue AS
SELECT DISTINCT ON (a.article_id)
       a.article_id, a.title, a.url_canonical, s.name AS source_name,
       a.published_at, v.score, v.tier, v.reason, a.retrieval_status
FROM core.article a
LEFT JOIN core.source s USING (source_id)
LEFT JOIN ml.relevance_verdict v USING (article_id)
ORDER BY a.article_id, v.created_at DESC;
