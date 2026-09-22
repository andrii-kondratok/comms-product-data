# Векторний відбір статей: як працює і як розгорнути

Щодня з ~2–3 тис. нових статей система відбирає **топ-30**, цікавий для ТМ.
Налаштовується оцінками людини («добре / погано»), а не жорсткими правилами в коді.

Результати перевірок — у [`docs/selection/`](../docs/selection/).

---

## Як це працює

```
RSS, sitemap, NewsCatcher ──► ops.candidate_pool          (discover_*, щогодини)
                                     │
                          bge-m3 ембединг заголовка+опису  (pgvector, 1024)
                                     │
      ┌──────────────────────────────┼───────────────────────────────┐
      ▼                              ▼                               ▼
 відповідність темі        схожість на взірці                 рутина (антитеми)
 23 теми редакції,         статті, що дали пости ТМ,          обстріли з жертвами,
 кілька формулювань        + позначені людиною «добре»        зведення, удари по НПЗ,
 на тему (topics.json)     (10 найближчих)                    ДТП — відсікаються
      └──────────────┬───────────────┘
                     ▼
          бал = ½ тема + ½ взірці
                     │
   свіжість ≤ 36 год · ≤ 5 статей на тему · одна стаття на подію
                     ▼
              marts.daily_top  ──►  (за потреби) 🧾 Articles, статус Linked
```

**Теми** — з документа редакції «Operational programming + strategic objectives»:
23 операційні теми, кожна прив'язана до стратегічних цілей (адвокація України, адвокація
KSE, особистий бренд, етична рамка). Шість тем, що народжуються всередині офісу (донори,
кампус, life content…), у відборі новин не беруть участі.

**Одна подія — одна стаття.** Статті зливаються, якщо схожість ≥ 0.70 (однією мовою) або
≥ 0.655 (між англ./укр./рос. — переклад знижує схожість). Колонка `event_size` — скільки
статей злилось у пункт топу: «про це пишуть N видань».

## Складові

| Що | Де |
|---|---|
| Теми, формулювання, рутина | `pipeline/topics.json` |
| Модель ембедингів | `pipeline/embeddings.py` (bge-m3, CPU) |
| Ознаки й дедуплікація | `pipeline/ranker.py` |
| Щогодинний топ | `pipeline/jobs/rank_daily.py` |
| Теми для кожної свіжої статті | `pipeline/jobs/score_topics.py` |
| Навчена модель (вимкнена) | `pipeline/jobs/train_ranker.py` |
| Схема | `db/migrations/0003`–`0007` |
| Перенесення топу в Notion | `posts_db/push_top_to_notion.py` |
| Перевірки | `posts_db/eval_topics.py`, `eval_threshold.py`, `eval_ranker.py`, `check_picks.py` |

Таблиці: `core.topic`, `core.topic_facet`, `core.routine_facet`, `core.topic_goal`,
`ml.candidate_embedding`, `ml.article_topic`, `ml.ranker_model`, `ml.daily_pick`,
`feedback.pick_feedback`. Вітрини: `marts.daily_top`, `marts.topic_queue`.

---

## Розгортання

Спершу — базове розгортання за [`DEPLOY.md`](DEPLOY.md). Відбір їде в тому самому
образі воркера; окремих сервісів немає.

### 1. Вимоги (важливо)

- **RAM 8 ГБ.** Модель bge-m3 займає ~2,5 ГБ у пам'яті воркера.
- **Диск: +2,3 ГБ** на модель у `deploy/data/hf` (том `data`). На локальній машині
  модель одного разу заповнила системний диск до нуля — стежте за місцем.
- **Доступ до** `huggingface.co` (модель, один раз) і `download.pytorch.org` (збірка).
- **CPU:** ~13 заголовків/с на 12 ядрах. 2–3 тис. кандидатів на годину — з запасом.

### 2. Оновлення коду і образу

```bash
cd /opt/comms && git pull
cd deploy && docker compose --env-file .env up -d --build
docker compose logs -f worker     # «застосовано: [...0003..0007]» і «планувальник запущено»
```

Міграції `0003`–`0007` застосовуються самі під час старту. Міграція `0007` вмикає
прозору формулу `transparent-v1` як активну модель.

### 3. Перший запуск — вручну, по черзі

Перший прогін довший за звичайний: качається модель і рахуються ембединги історії.

```bash
docker compose exec worker python -m pipeline run discover_rss      # свіжі кандидати
docker compose exec worker python -m pipeline run score_topics      # модель (~2,3 ГБ) + теми
docker compose exec worker python -m pipeline run rank_daily        # ~3 тис. взірців + топ
```

Очікувано: `score_topics` — `facets_added: 74, routine_added: 21` (на чистій базі);
`rank_daily` — `history_embedded` ≈ 3–5 тис. при першому запуску, далі 0; `picked: 30`.

### 4. Перевірка

```sql
SELECT rank, event_size, topic, domain, left(title, 90)
FROM marts.daily_top WHERE day = current_date ORDER BY rank;

SELECT version, is_active, params FROM ml.ranker_model;          -- активна: transparent-v1
SELECT job, last_status, stats FROM marts.pipeline_health
WHERE job IN ('score_topics', 'rank_daily');
```

Ознаки проблеми: `picked` менше 30 (мало кандидатів — перевірте `discover_rss`),
`history_embedded` = 0 при першому запуску (немає історії — чи пройшов `backfill`).

### 5. Що далі робить розклад

| Задача | Коли |
|---|---|
| `score_topics` | :15 і :45 — теми для свіжих статей |
| `rank_daily` | :55 — новий зріз топу (попередні зберігаються в `ml.daily_pick`) |
| `train_ranker` | понеділок 04:00 — навчає регресію, але **не вмикає** її (див. нижче) |

---

## Щоденна робота: оцінки

Оцінки — головний спосіб налаштування. `good` робить статтю взірцем: з **наступного дня**
схожі статті піднімаються. `bad` фіксується для аналізу й навчання.

**Найпростіше — через перенесення в Notion:**

```bash
# подивитись, що піде в Articles
docker compose exec worker python posts_db/push_top_to_notion.py --exclude 6,20,24
# перенести; виключені номери → bad, свої посилання з файлу → good
docker compose exec worker python posts_db/push_top_to_notion.py --exclude 6,20,24 \
    --extra /app/data/my_urls.txt --apply
```

**Або напряму в базі:**

```sql
INSERT INTO feedback.pick_feedback (day, candidate_id, verdict, reason, reviewer, rank_shown)
SELECT p.day, p.candidate_id, 'bad', 'річниця — не новина', 'Andrii', p.rank
FROM ml.daily_pick p
WHERE p.day = current_date
  AND p.computed_at = (SELECT max(computed_at) FROM ml.daily_pick WHERE day = current_date)
  AND p.rank IN (6, 24);
```

Дублікат (дві статті про одну подію в топі) — `verdict = 'duplicate'`, `duplicate_of` = id
іншої: це вада дедуплікації, у навчання не йде.

**Своя стаття, якої немає в топі:** `posts_db/check_picks.py файл_з_посиланнями --record`
покаже, де вона в рейтингу, і запише як `good`.

---

## Налаштування без коду

| Хочу | Що змінити |
|---|---|
| Прибрати тип новин («щоденні удари по НПЗ») | додати формулювання в `routine` у `topics.json` |
| Точніше описати тему | правити `facets` теми в `topics.json` |
| Більше/менше однієї теми в топі | `params.topic_cap` у `ml.ranker_model` |
| Вага теми проти взірців | `params.w_topic`, `params.w_knn` |
| Вікно свіжості | `params.fresh_hours` |
| Розмір топу | `DAILY_TOP` у `deploy/.env` |

```sql
UPDATE ml.ranker_model
SET params = jsonb_set(params, '{topic_cap}', '4')
WHERE version = 'transparent-v1';
```

Після правки `topics.json`: `git pull` + перезапуск воркера. Версія таксономії рахується
з хешу формулювань — свіжі статті переоціняться самі, старі бали з новими не змішуються.

**Пастка, на яку вже наступали:** якщо формулювання теми й рутини майже однакові
(«удар дронів по НПЗ» і там, і там), рутина не спрацює — стаття однаково близька до обох.
Прибирайте конфліктне формулювання з теми.

---

## Навчена модель (вимкнена) і чому

`train_ranker` щотижня навчає логістичну регресію на виборі редакції + оцінках і пише
її в `ml.ranker_model` з метриками, але **не активує**. Перевірка людиною 22.09 показала:
на 2–3 днях даних вона вчить шум (від'ємна вага відповідності темі, Reuters тоне за
джерелом). Вмикати варто, коли набереться кілька тижнів оцінок і вона обійде прозору
формулу на відкладених днях:

```sql
UPDATE ml.ranker_model SET is_active = false WHERE is_active;
UPDATE ml.ranker_model SET is_active = true WHERE version = 'lr-...';
```

Повернутись назад — те саме з `transparent-v1`.

---

## Відомі обмеження

- **Частина джерел поза пулом.** 4 з 20 ручних статей 22.09 (Politico.eu, WaPo, Guardian)
  стрічки не дали — топ їх не побачить у принципі. Потрібні додаткові стрічки/розділи.
- **Тема біля статті іноді хибна** при правильному відборі (хакери → «академічна
  доброчесність»). На відбір не впливає, на звіти «скільки на яку ціль» — впливає.
- **Пороги калібровано на кількох днях.** Через тиждень роботи перерахувати:
  `python posts_db/eval_threshold.py` і `check_picks.py` на нових ручних відборах.
- **Локально на Windows** підключайтесь до бази через `127.0.0.1`, не `localhost`:
  через IPv6 кожне з'єднання висить 130 с.
