# Воркер будується на тому ж образі, що й база: pg_dump гарантовано версії 16,
# і не треба окремого репозиторію PostgreSQL (apt.postgresql.org буває недоступний).
FROM pgvector/pgvector:pg16

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip python3-psycopg python3-lxml ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# venv бачить системний psycopg; з PyPI ставиться лише trafilatura.
# INSTALL_EXTRACTOR=0 — збірка без PyPI (локальний тест у мережі, де PyPI закритий).
ARG INSTALL_EXTRACTOR=1
RUN python3 -m venv --system-site-packages /opt/venv \
 && if [ "$INSTALL_EXTRACTOR" = "1" ]; then /opt/venv/bin/pip install --no-cache-dir "trafilatura>=2.0,<3"; fi

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PYTHONUTF8=1 TZ=Europe/Kyiv
WORKDIR /app

COPY db/ db/
COPY pipeline/ pipeline/
COPY postgres/ postgres/
COPY posts_db/sources.csv posts_db/sources.csv

# Базовий образ стартує postgres — воркеру він не потрібен
ENTRYPOINT []
USER postgres
CMD ["python", "-m", "pipeline", "scheduler"]
