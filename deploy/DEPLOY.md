# Розгортання: база + щоденний конвеєр

Один сервер, два контейнери: `db` (Postgres 16 + pgvector) і `worker`, у якому живе
планувальник. Розклад зберігається в таблиці `ops.job`: щоб змінити частоту чи вимкнути
задачу, досить зробити UPDATE, перезапускати нічого не треба.

Відбір статей (топ дня, ембединги, оцінки) описано окремо — [SELECTION.md](SELECTION.md).

## Вимоги до сервера

- Linux із Docker Engine і плагіном `docker compose`
- 4 vCPU, 8 ГБ RAM, 50 ГБ диска. Модель ембедингів bge-m3 займає ~2,5 ГБ пам'яті й
  ~2,3 ГБ на диску (качається при першому запуску `score_topics` у `deploy/data/hf`).
  База зараз ~1 ГБ, приріст ~1–2 ГБ на місяць
- доступ у інтернет на 443 до `api.notion.com`, `v3-api.newscatcherapi.com`, `t.co`,
  сайтів джерел, `pypi.org` / `files.pythonhosted.org` / `download.pytorch.org` (для збірки)
  і `huggingface.co` (модель ембедингів)

## 1. Код і дані на сервер

```bash
# код
sudo mkdir -p /opt/comms && sudo chown $USER /opt/comms
git clone https://github.com/andrii-kondratok/comms-product-data.git /opt/comms

# дані для разового завантаження історії — архів comms-data-2026-09-18.tar.gz
# (61 МБ, передається окремо від GitHub: там повні тексти статей і постів)
sha256sum comms-data-2026-09-18.tar.gz
# має бути f6a7890652f5939b2cd01a4339dad44d941c80913e327a673e00d50d6a133bcb
mkdir -p /opt/comms/deploy/data /opt/comms/deploy/backups
tar -xzf comms-data-2026-09-18.tar.gz -C /opt/comms/deploy/data

# том data монтується у воркер від користувача postgres (uid 999)
sudo chown -R 999:999 /opt/comms/deploy/data /opt/comms/deploy/backups
```

Усередині архіву `processed/` і `raw/`: статті з текстами, пари, пул кандидатів,
знімок Post Metrics і кеш тіл Content Pulse (щоб воркер збагачення не качав їх заново).

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
| `score_topics` | :15 і :45 | ембединги свіжих статей (pgvector) і найближчі теми редакції |
| `rank_daily` | щогодини, :55 | топ-30 дня: тема + схожість на взірці, без рутини, одна стаття на подію |
| `train_ranker` | понеділок, 04:00 | навчає регресію на виборі редакції й оцінках; сама не вмикається (див. SELECTION.md) |
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
- `marts.topic_queue`: свіжі статті з найближчою темою, цілями і прапорцем «вище порогу»
- `marts.daily_top`: топ-30 дня (останній зріз) — головна вітрина для редактора
- `ml.ranker_model`: версії реранкера, на яких днях навчені, метрики на відкладеному дні

Перший запуск відбору (модель ембедингів, взірці) — вручну, по черзі: кроки в
[SELECTION.md](SELECTION.md#3-перший-запуск--вручну-по-черзі). Навчати реранкер не треба:
активна прозора формула `transparent-v1`.

Тривожні ознаки: `consecutive_failures >= 3` або `since_last_finish` більше за два
інтервали розкладу.

## Що не зроблено й варто додати

- копія бекапів за межі сервера (S3 або інший хост): зараз вони лежать на тому самому диску
- сповіщення про падіння задач (Slack або пошта)
- ключ NewsCatcher тріальний: коли він закінчиться, `discover_newscatcher` почне
  падати, а в `fetch_articles` антибот-домени стануть `blocked`. Решта працюватиме
