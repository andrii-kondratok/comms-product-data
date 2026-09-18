# Лист колезі: що вивантажити з BigQuery

Готовий текст. Надсилати як є, технічну частину можна лишити під катом.

---

## Текст повідомлення

Привіт! Дякую за доступ і за CSV — він уже дав користь, зібрав із нього 9 902 треди. Але вперлися у дві речі, і обидві вирішуються з твого боку за кілька хвилин.

**1. Таблиця в BigQuery зникла сама.**

`x_archive.tweets` більше немає. Це не хтось видалив — на датасеті стоїть дефолтне протермінування таблиць:

```
default_table_expiration_ms = 5184000000   (60 днів)
```

Датасет створено 5 червня, таблиця протермінувалася **4 серпня**. Останній запит, який реально читав дані, був 4 серпня о 07:59 — обробив 30 МБ. Усе після нього віддавало кеш. Time travel не допоміг: вікно 7 днів, а минуло вже 39.

Перед тим, як заливати знову, варто прибрати протермінування, інакше через 60 днів повториться:

```sql
ALTER SCHEMA `x-analysis-498513.x_archive`
SET OPTIONS (default_table_expiration_days = NULL);
```

**2. У CSV немає зв'язків між твітами.**

Колонка `thread_id` містить id самого твіта, а не треду: 56 972 унікальних значення на 56 972 рядки, і твіти одного треду ніколи не мають спільного значення. Тому треди довелося збирати за авторською нумерацією (`1/`, `2/`, `6X`) — працює, але приблизно.

Схоже також, що вибірка неповна: 56 972 твіти при 15 374 відомих постах дають 3.7 твіта на пост, тоді як за нумерацією виходить ~6. Знайшли 7 379 місць, де між `3/` і `5/` бракує самого `4/` — усього близько 7 500 пропущених твітів.

**Що потрібно.** Повне вивантаження, без `LIMIT` і без `DISTINCT`, із полями зв'язку.

Обов'язково:

| Поле | Навіщо |
|---|---|
| `id` | ключ твіта |
| `conversation_id` | ← **головне.** Збирає треди точно, замість вгадування за нумерацією |
| `in_reply_to_status_id` | якщо `conversation_id` немає — відновлює ланцюг |
| `author_id` | відсіяти чужі твіти всередині тредів |
| `created_at` | порядок і дедуплікація |
| `full_text` | повний текст, не обрізаний |
| `is_retweet` / `referenced_tweets` | відсіяти ретвіти |

Бажано:

| Поле | Навіщо |
|---|---|
| `lang` | фільтр мови |
| `likes`, `retweets`, `replies`, `quotes`, `impressions` | метрики на рівні твіта |
| `in_reply_to_user_id` | відрізнити самовідповідь від відповіді комусь |
| `quoted_status_id` | цитування |
| `media_urls`, `urls` | посилання на джерела — потрібні окремо |

**Як віддати.** Найпростіше — просто лишити таблицю в BigQuery, у нас є ключ сервісного акаунта, заберемо самі. CSV не потрібен: через нього ми втрачаємо типи, а на експорті ще й частину рядків.

Якщо оригінал лежить у файлах, найшвидше так:

```bash
bq load --autodetect --source_format=NEWLINE_DELIMITED_JSON \
  x-analysis-498513:x_archive.tweets_raw  'gs://<бакет>/tweets*.json'
```

Головне — **не фільтрувати на завантаженні**. Відповіді, ретвіти й службові твіти ми відсіємо самі; краще мати зайве, ніж дізнатися через місяць, що чогось бракує.

Скажи, коли буде — заберу того ж дня.

---

## Технічний додаток (за потреби)

Що ми знаємо про схему старої таблиці зі збереженого SQL:

```sql
SELECT DISTINCT
  CAST(id AS STRING) AS tweet_id, text, likes, retweets, created_at
FROM `x-analysis-498513.x_archive.tweets`
WHERE id IS NOT NULL
```

Два зауваження до цього запиту, якщо він же використовувався для експорту в CSV:

- `DISTINCT` по цих п'яти полях **зливає різні твіти з однаковим текстом** — наприклад, повтори однієї фрази в різних тредах. Частина пропусків може бути звідси.
- `text` замість `full_text` у деяких схемах дає обрізаний на 140 знаків варіант. У CSV максимальна довжина — 399 знаків, тож тут, схоже, гаразд, але варто перевірити.

Цільова структура, якщо є з чого зібрати:

```sql
CREATE OR REPLACE TABLE `x-analysis-498513.x_archive.tweets` AS
SELECT
  CAST(id AS STRING)                    AS tweet_id,
  CAST(conversation_id AS STRING)       AS conversation_id,
  CAST(in_reply_to_status_id AS STRING) AS in_reply_to_status_id,
  CAST(in_reply_to_user_id AS STRING)   AS in_reply_to_user_id,
  CAST(author_id AS STRING)             AS author_id,
  created_at,
  full_text,
  lang,
  is_retweet,
  CAST(quoted_status_id AS STRING)      AS quoted_status_id,
  likes, retweets, replies, quotes, impressions,
  media_urls, urls
FROM `<джерело>`;
```

Перевірка після заливки — має бути приблизно так:

```sql
SELECT
  COUNT(*)                                   AS tweets,
  COUNT(DISTINCT conversation_id)            AS threads,
  MIN(created_at)                            AS first_tweet,
  MAX(created_at)                            AS last_tweet,
  COUNTIF(conversation_id IS NULL)           AS no_conversation_id,
  COUNTIF(full_text IS NULL OR full_text='')  AS empty_text
FROM `x-analysis-498513.x_archive.tweets`;
```

Очікуємо помітно більше за 56 972 твіти й діапазон із лютого 2022 по сьогодні.
