-- База опублікованих постів ТМ.
-- Діалект DuckDB (сумісний з Postgres) — переноситься в Postgres без переписування.
--
-- Принципи:
--   * сирий текст ніколи не перезаписується: text_raw лишається як у джерелі
--   * будь-яка нормалізація — окрема колонка, щоб рішення можна було переглянути
--   * кожен рядок знає, з якого файлу прийшов (провенанс)
--   * метрики — довгий формат зі знімком у часі, бо їх збирають кілька конвеєрів

DROP TABLE IF EXISTS post_metric;
DROP TABLE IF EXISTS load_issue;
DROP TABLE IF EXISTS post;
DROP TABLE IF EXISTS source_file;

CREATE TABLE source_file (
    source_file_id  INTEGER PRIMARY KEY,
    path            TEXT    NOT NULL,
    kind            TEXT    NOT NULL,   -- fb_export | x_hooks | x_recent | notion
    file_mtime      TIMESTAMP,
    rows_read       INTEGER,
    rows_loaded     INTEGER,
    loaded_at       TIMESTAMP NOT NULL
);

CREATE TABLE post (
    post_id         TEXT    PRIMARY KEY,   -- '<platform>:<native_id>'
    platform        TEXT    NOT NULL,      -- X | FB
    native_id       TEXT,                  -- status id / story_fbid
    url             TEXT,
    url_canonical   TEXT,
    posted_at       TIMESTAMP,             -- UTC
    posted_date     DATE,

    -- Текст. hook = перший твіт треду (X) або весь допис (FB).
    hook_raw        TEXT,                  -- як у джерелі, з маркерами
    hook_clean      TEXT,                  -- без маркерів нумерації
    body_raw        TEXT,                  -- повний тред, коли відомий (поки NULL для X)
    numbering_style TEXT,                  -- leading | trailing | none
    hook_chars      INTEGER,
    has_newline     BOOLEAN,
    is_truncated_suspect BOOLEAN,          -- рівно на межі 200 знаків

    post_type       TEXT,
    source_file_id  INTEGER REFERENCES source_file(source_file_id),
    ingested_at     TIMESTAMP NOT NULL
);

CREATE TABLE post_metric (
    post_id         TEXT NOT NULL REFERENCES post(post_id),
    metric          TEXT NOT NULL,         -- impressions | likes | comments | shares | reach | views
    value           DOUBLE,
    source_file_id  INTEGER REFERENCES source_file(source_file_id),
    observed_at     TIMESTAMP,
    PRIMARY KEY (post_id, metric, source_file_id)
);

-- Усе, що не вдалося завантажити чисто, лишається видимим, а не зникає.
CREATE TABLE load_issue (
    source_file_id  INTEGER REFERENCES source_file(source_file_id),
    row_index       INTEGER,
    issue           TEXT NOT NULL,
    detail          TEXT
);
