"""Batch-оценка качества гео-пайплайна на реальных сообщениях.

Запуск из корня проекта:
    .venv-refactor/bin/python scripts/evaluate_live_messages.py [файл]

По умолчанию берёт live_messages.txt (одно сообщение на строку).
Использует тот же оффлайн-фикстурный путь, что и tests/test_street_matcher.py:
индекс строится из postgres/data/geo.csv без БД.

Выход:
    debug/eval_results.jsonl — результат по каждому сообщению
    debug/eval_report.json   — агрегированные метрики
    stdout — краткая сводка
"""

import asyncio
import csv
import json
import re
import sys
import types
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Стаб-пакеты как в тестах: parser/__init__.py тянет asyncpg/kurigram.
if "parser" not in sys.modules:
    _pkg = types.ModuleType("parser")
    _pkg.__path__ = [str(ROOT / "parser")]
    sys.modules["parser"] = _pkg
if "processor" not in sys.modules:
    _pkg = types.ModuleType("processor")
    _pkg.__path__ = [str(ROOT / "processor")]
    sys.modules["processor"] = _pkg

from processor.morphology import Morphology              # noqa: E402
from processor.phonetic_index import PhoneticIndex       # noqa: E402
from processor.geo_matcher import GeoMatcher             # noqa: E402
from processor.word_tokenizer import tokenize            # noqa: E402
from common.text_preprocessor import (                   # noqa: E402
    preprocess_light, strip_tail, is_promotional,
)
from common.settings import settings                     # noqa: E402

DEFAULT_INPUT = ROOT / "live_messages.txt"
OUT_JSONL = ROOT / "eval_results.jsonl"
OUT_JSON = ROOT / "eval_report.json"

URL_RE = re.compile(r"https?://|t\.me/")
QUESTION_RE = re.compile(r"\?|^\s*(как|где|что|кто|почему|уточните|подскажите)", re.IGNORECASE)

MIN_CONF = settings.geo.candidate_min_score  # 0.80 — SQL-порог
WEAK_LO, WEAK_HI = 0.70, MIN_CONF            # серая зона, отрезаемая SQL


def load_messages(path: Path) -> list[str]:
    msgs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                msgs.append(line)
    return msgs


def load_geo_and_stopwords():
    csv.field_size_limit(10_000_000)
    rows, name2id = [], {}
    with open(ROOT / "postgres/data/geo.csv", encoding="utf-8") as f:
        rd = csv.reader(f)
        next(rd)
        gid = 0
        for r in rd:
            if not r or not r[0].strip():
                continue
            gid += 1
            names = r[0].split("|")
            rows.append({"id": gid, "names": names})
            name2id.setdefault(names[0], gid)
    stop = set()
    with open(ROOT / "postgres/data/stopwords.csv", encoding="utf-8") as f:
        rd = csv.reader(f)
        next(rd)
        for r in rd:
            if r and r[0].strip():
                stop.add(r[0].strip().lower())
    return rows, name2id, stop


async def process_one(matcher, morph, i: int, text: str) -> dict:
    """Обработать одно сообщение и вернуть JSON-совместимую запись результата."""
    norm = preprocess_light(strip_tail(text))
    toks = tokenize(norm)
    lemmas = morph.lemmatize_tokens(toks)
    try:
        ents = await matcher.find_geo(tokens=toks, lemmas=lemmas)
    except Exception as e:  # noqa: BLE001 — отчёт не должен падать
        ents = []
        print(f"[{i}] EXCEPTION: {e!r}", flush=True)
    conf = [e for e in ents if e["score"] >= MIN_CONF]
    weak = [e for e in ents if WEAK_LO <= e["score"] < MIN_CONF]

    category = "matched" if conf else ("weak_only" if weak else "none")

    def _clean(es):
        return [
            {k: (round(v, 4) if isinstance(v, float) else v)
             for k, v in e.items() if k in ("geo_id", "matched_name", "score", "source", "type")}
            for e in es
        ]

    return {
        "i": i,
        "text": text,
        "norm": norm,
        "category": category,
        "confident": _clean(conf),
        "weak": _clean(weak),
        "flags": {
            "url": bool(URL_RE.search(text)),
            "promo": bool(is_promotional(text)),
            "question": bool(QUESTION_RE.search(text)) if not conf else False,
            "empty_norm": len(norm.strip()) == 0,
        },
    }


def _fountanka_ids(name2id) -> set:
    return {gid for nm, gid in name2id.items() if "фонтанка" in nm.lower()}


def build_report(results: list, path: str, name2id=None) -> dict:
    counters = {"matched": 0, "weak_only": 0, "none": 0}
    name_counter: Counter = Counter()
    type_counter: Counter = Counter()
    source_counter: Counter = Counter()
    score_buckets = Counter({"conf>=0.90": 0, "conf 0.80-0.90": 0, "weak 0.70-0.80": 0, "sub 0.70": 0})
    ent_per_msg: Counter = Counter()
    fountanka_plain = 0

    fids = _fountanka_ids(name2id) if name2id else set()
    for r in results:
        conf = r["confident"]
        weak = r["weak"]
        counters[r["category"]] += 1
        for e in conf + weak:
            name_counter[e["matched_name"]] += 1
            type_counter[e.get("type", "?")] += 1
            source_counter[e.get("source", "?")] += 1
            s = e["score"]
            if s >= 0.90:
                score_buckets["conf>=0.90"] += 1
            elif s >= MIN_CONF:
                score_buckets["conf 0.80-0.90"] += 1
            elif s >= WEAK_LO:
                score_buckets["weak 0.70-0.80"] += 1
            else:
                score_buckets["sub 0.70"] += 1
        ent_per_msg[len(conf)] += 1
        if fids and any(e["geo_id"] in fids for e in conf):
            fountanka_plain += 1

    total = len(results)
    none_msgs = [r for r in results if r["category"] == "none"]
    weak_msgs = [r for r in results if r["category"] == "weak_only"]
    none_breakdown = Counter(
        "url" if r["flags"]["url"] else
        "promo" if r["flags"]["promo"] else
        "empty" if r["flags"]["empty_norm"] else
        "question" if r["flags"]["question"] else
        "text_no_geo"
        for r in none_msgs
    )
    return {
        "input": str(path),
        "total_messages": total,
        "min_confident_score": MIN_CONF,
        "categories": counters,
        "coverage_pct": round(100.0 * counters["matched"] / max(total, 1), 1),
        "entities_total": sum(name_counter.values()),
        "entity_type_distribution": dict(type_counter),
        "entity_source_distribution": dict(source_counter),
        "score_buckets": dict(score_buckets),
        "confident_entities_per_message": {str(k): v for k, v in sorted(ent_per_msg.items())},
        "top_matched_names": [{"name": n, "count": c} for n, c in name_counter.most_common(40)],
        "none_breakdown": dict(none_breakdown),
        "fountanka_plain_hits": fountanka_plain,
        "fountanka_plain_samples": [r["text"] for r in results if any(
            e["geo_id"] in fids for e in r["confident"])][:15] if fids else [],
        "weak_samples": [{"text": r["text"], "weak": r["weak"]} for r in weak_msgs[:60]],
        "none_samples": [{"text": r["text"], "flags": r["flags"]} for r in none_msgs[:60]],
    }


async def run() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    messages = load_messages(path)
    print(f"Loaded {len(messages)} messages from {path}", flush=True)

    morph = Morphology()
    index = PhoneticIndex(morph)
    rows, name2id, stop = load_geo_and_stopwords()
    index.build(rows)
    matcher = GeoMatcher(morph, index)
    matcher._initialized = True
    matcher._stopwords = stop
    print(f"Index built: {index.size if hasattr(index, 'size') else 'ok'}", flush=True)

    results = []
    counters = {
        "matched": 0,      # ≥1 сущность с score >= MIN_CONF
        "weak_only": 0,    # есть сущности, но все в серой зоне 0.70–0.80
        "none": 0,         # ничего не найдено
    }
    name_counter: Counter = Counter()
    type_counter: Counter = Counter()
    source_counter: Counter = Counter()
    score_buckets = Counter({"conf>=0.90": 0, "conf 0.80-0.90": 0, "weak 0.70-0.80": 0, "sub 0.70": 0})
    ent_per_msg = Counter()

    fountanka_ids = {gid for nm, gid in name2id.items() if "Фонтанка" in nm or "фонтанка" in nm.lower()}

    with open(OUT_JSONL, "w", encoding="utf-8") as out:
        for i, text in enumerate(messages, 1):
            rec = await process_one(matcher, morph, i, text)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            results.append(rec)
            if i % 200 == 0:
                print(f"  ... {i}/{len(messages)} processed", flush=True)

    report = build_report(results, str(path), name2id)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("weak_samples", "none_samples",
                                   "fountanka_plain_samples", "top_matched_names")},
                     ensure_ascii=False, indent=2))
    print("\nTop-30 matched names:")
    for m in report["top_matched_names"][:30]:
        print(f"  {m['count']:4d}  {m['name']}")


if __name__ == "__main__":
    asyncio.run(run())
