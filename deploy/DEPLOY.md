# Розгортання: база + щоденний конвеєр

Один сервер, два контейнери: `db` (Postgres 16 + pgvector) і `worker`, у якому живе
планувальник. Розклад зберігається в таблиці `ops.job`: щоб змінити частоту чи вимкнути
задачу, досить зробити UPDATE, перезапускати нічого не треба.

## Вимоги до сервера

- Linux із Docker Engine і плагіном `docker compose`
- 2 vCPU, 4 ГБ RAM, 40 ГБ диска. Зараз база займає ~1 ГБ, приріст ~1–2 ГБ на місяць
- доступ у інтернет на 443 до `api.notion.com`, `v3-api.newscatcherapi.com`, `t.co`,
  сайтів джерел і `pypi.org` / `files.pythonhosted.org` (для збірки)

## 1. Код і дані на сервер

```bash
# з локальної машини, з кореня проєкту: код
rsync -av --exclude data --exclude backups --exclude '*.duckdb' --exclude '.env*' \
      ./ user@SERVER:/opt/comms/

# дані для разового завантаження історії (~250 МБ), лише потрібні файли
ssh user@SERVER 'mkdir -p /opt/comms/deploy/data/processed /opt/comms/deploy/data/raw/notion'
rsync -av data/processed/{pg_article,training_pairs,training_pairs_todo,sitemaps,candidate_pool_export}.csv \
      user@SERVER:/opt/comms/deploy/data/processed/
rsync -av data/raw/{fetched_articles,newscatcher_v3}.jsonl user@SERVER:/opt/comms/deploy/data/raw/
rsync -av data/raw/notion/_pm_live.json user@SERVER:/opt/comms/deploy/data/raw/notion/

# том data монтується у воркер від користувача postgres (uid 999)
ssh user@SERVER 'sudo chown -R 999:999 /opt/comms/deploy/data && mkdir -p /opt/comms/deploy/backups && sudo chown 999:999 /opt/comms/deploy/backups'
```

Якщо на машині немає rsync (Windows), те саме можна зробити через `scp`.

## 2. Секрети

```bash
cd /opt/comms/deploy
cp .env.example .env
chmod 600 .env
# заповнити: POSTGRES_PASSWORD (довгий випадковий), NOTION_TOKEN, NEWSCATCHER_V3_KEY
openssl rand -base64 32   # для POSTGRES_PASSWORD
```

## 3. Запуск

```bash
cd /opt/comms/deploy
docker compose --env-file .env up -d --build
docker compose logs -f worker          # має бути «планувальник запущено»
```

Міграції застосовуються автоматично під час старту планувальника.

## 4. Разове завантаження історії

```bash
docker compose exec worker python -m pipeline backfill
docker compose exec worker python -m pipeline status
```

Команду можна повторювати: вона ідемпотентна.

## 5. Перевірка

```bash
# прогнати кожну задачу вручну один раз
for j in discover_rss sync_notion fetch_articles link_posts reconcile_candidates; do
  docker compose exec worker python -m pipeline run $j
done
docker compose exec db psql -U comms -d comms -c "SELECT * FROM marts.pipeline_health"
```

## Розклад (час Києва)

| Задача | Коли | Що робить |
|---|---|---|
| `discover_rss` | щогодини, :05 | RSS і news-sitemap усіх джерел → пул кандидатів |
| `discover_newscatcher` | 07:00, 13:00, 19:00 | 8 тематичних запитів за 24 год, з кластерами |
| `fetch_articles` | кожні 30 хв | текст свіжих статей за каскадом із реєстру, з повторами |
| `sync_notion` | кожні 15 хв | Post Metrics, Articles, Content Pulse — лише змінене |
| `enrich_post_metrics` | кожні 15 хв | Post text і Source у Post Metrics |
| `link_posts` | щогодини, :40 | пари стаття → пост із явних посилань |
| `reconcile_candidates` | щогодини, :50 | кандидат у дайджесті → `ingested` |
| `backup` | 03:30 | `pg_dump` у `deploy/backups`, зберігається 14 днів |

Змінити розклад або вимкнути задачу:

```sql
UPDATE ops.job SET schedule = '0 */2 * * *' WHERE job = 'discover_rss';
UPDATE ops.job SET enabled = false WHERE job = 'discover_newscatcher';
```

## Доступ до бази

Postgres слухає лише `127.0.0.1` сервера. Підключатись через SSH-тунель:

```bash
ssh -L 5432:127.0.0.1:5432 user@SERVER
# далі DBeaver / psql на localhost:5432
```

## Моніторинг

- `marts.pipeline_health`: стан кожної задачі і скільки помилок поспіль
- `marts.daily_intake`: скільки кандидатів і текстів прийшло за день по джерелах
- `marts.api_usage_daily`: витрата запитів NewsCatcher проти ліміту

Тривожні ознаки: `consecutive_failures >= 3` або `since_last_finish` більше за два
інтервали розкладу.

## Що не зроблено й варто додати

- копія бекапів за межі сервера (S3 або інший хост): зараз вони лежать на тому самому диску
- сповіщення про падіння задач (Slack або пошта)
- ключ NewsCatcher тріальний: коли він закінчиться, `discover_newscatcher` почне
  падати, а в `fetch_articles` антибот-домени стануть `blocked`. Решта працюватиме
