"""Налаштування з оточення. Секрети лише звідси, у коді й логах їх немає."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Для локального запуску: .env у корені проєкту. На сервері — змінні compose."""
    for name in (".env", ".env.local"):
        f = ROOT / name
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# Кеш моделей — у томі data, а не в домашній теці: модель ембедингів важить ~2,3 ГБ,
# і на системному диску вона вже раз забила місце до нуля.
os.environ.setdefault("HF_HOME", str(ROOT / "data" / "hf"))

# 127.0.0.1, а не localhost: на Windows з Docker Desktop localhost спершу пробує IPv6
# і висить 130 с до таймауту на кожне з'єднання. На сервері адреса інша (db).
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://comms:comms@127.0.0.1:5432/comms")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NEWSCATCHER_V3_KEY = os.environ.get("NEWSCATCHER_V3_KEY", "")
PIPELINE_VERSION = os.environ.get("PIPELINE_VERSION", "dev")
TZ = os.environ.get("PIPELINE_TZ", "Europe/Kyiv")
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", str(ROOT / "backups")))

# Бюджет провайдера: тріал рахує запити, тому ліміт на день явний
NEWSCATCHER_MAX_CALLS_PER_DAY = int(os.environ.get("NEWSCATCHER_MAX_CALLS_PER_DAY", "60"))

# Notion data sources (database ids)
NOTION_DB = {
    "content_pulse": "bd9d1fbd-7688-825d-84d5-8102fcb47d15",
    "post_metrics":  "570dde03-224a-4f17-8fd3-bbab048e8ca0",
    "articles":      "7c06f91f-100f-4202-8f74-135e547aae44",
}
