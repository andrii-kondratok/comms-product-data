"""Ембединги: модель, завантаження тем, запис векторів у pgvector.

Модель bge-m3: мультимовна (англ./укр./рос. — усі мови наших джерел),
1024 виміри під колонки vector(1024) у схемі. Працює на CPU.
Модель вантажиться один раз на процес планувальника.
"""

from __future__ import annotations

import hashlib
import json
import os

from . import config

MODEL_NAME = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
TOPICS_FILE = config.ROOT / "pipeline" / "topics.json"
_model = None


def model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer   # важкий імпорт — лише тут
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model


def encode(texts: list):
    return model().encode(texts, batch_size=32, normalize_embeddings=True,
                          convert_to_numpy=True, show_progress_bar=False)


def vec(v) -> str:
    """numpy → літерал pgvector без окремої залежності pgvector-python."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def taxonomy_version(d: dict) -> str:
    """Хеш усього, що впливає на бали: фасети тем і антитеми рутини."""
    key = json.dumps({"topics": [(t["code"], t["from_news"], t["facets"]) for t in d["topics"]],
                      "routine": d.get("routine", []), "model": MODEL_NAME},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def sync_topics(con) -> dict:
    """topics.json → core.topic / topic_goal / topic_facet / routine_facet.

    Ембединги рахуються лише для нових формулювань. Версія таксономії пишеться
    в ml.topic_threshold: вітрина показує бали лише поточної версії.
    """
    d = json.load(TOPICS_FILE.open(encoding="utf-8"))
    for code, g in d["goals"].items():
        con.execute("""INSERT INTO core.strategic_goal (goal_code, name_uk, pm_labels)
                       VALUES (%s,%s,%s) ON CONFLICT (goal_code) DO UPDATE
                       SET name_uk=EXCLUDED.name_uk, pm_labels=EXCLUDED.pm_labels""",
                    (code, g["name_uk"], g.get("pm_label", [])))
    codes = []
    for t in d["topics"]:
        codes.append(t["code"])
        con.execute("""INSERT INTO core.topic (topic_code, topic_no, block, name_uk, from_news)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT (topic_code) DO UPDATE
                       SET topic_no=EXCLUDED.topic_no, block=EXCLUDED.block,
                           name_uk=EXCLUDED.name_uk, from_news=EXCLUDED.from_news,
                           is_active=true, updated_at=now()""",
                    (t["code"], t["id"], t["block"], t["name_uk"], t["from_news"]))
        con.execute("DELETE FROM core.topic_goal WHERE topic_code=%s", (t["code"],))
        for g in t["goals"]:
            con.execute("INSERT INTO core.topic_goal VALUES (%s,%s)", (t["code"], g))
    con.execute("UPDATE core.topic SET is_active=false WHERE NOT (topic_code = ANY(%s))", (codes,))

    # фасети: прибрати ті, яких більше немає у файлі; додати нові
    wanted = {(t["code"], f) for t in d["topics"] for f in t["facets"]}
    have = {(r["topic_code"], r["text"]) for r in con.execute(
        "SELECT topic_code, text FROM core.topic_facet WHERE model=%s", (MODEL_NAME,))}
    for code, text in have - wanted:
        con.execute("DELETE FROM core.topic_facet WHERE topic_code=%s AND text=%s AND model=%s",
                    (code, text, MODEL_NAME))
    new = sorted(wanted - have)
    if new:
        for (code, text), e in zip(new, encode([t for _, t in new])):
            con.execute("""INSERT INTO core.topic_facet (topic_code, text, model, embedding)
                           VALUES (%s,%s,%s,%s::vector)""", (code, text, MODEL_NAME, vec(e)))
    # антитеми рутини
    r_wanted = set(d.get("routine", []))
    r_have = {r["text"] for r in con.execute(
        "SELECT text FROM core.routine_facet WHERE model=%s", (MODEL_NAME,))}
    for text in r_have - r_wanted:
        con.execute("DELETE FROM core.routine_facet WHERE text=%s AND model=%s", (text, MODEL_NAME))
    r_new = sorted(r_wanted - r_have)
    if r_new:
        for text, e in zip(r_new, encode(r_new)):
            con.execute("""INSERT INTO core.routine_facet (text, model, embedding)
                           VALUES (%s,%s,%s::vector)""", (text, MODEL_NAME, vec(e)))

    version = taxonomy_version(d)
    con.execute("UPDATE ml.topic_threshold SET taxonomy_version=%s WHERE model=%s",
                (version, MODEL_NAME))
    return {"topics": len(codes), "facets_added": len(new),
            "facets_removed": len(have - wanted), "routine_added": len(r_new),
            "taxonomy_version": version}
