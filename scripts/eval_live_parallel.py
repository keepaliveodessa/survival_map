"""Параллельная batch-оценка пайплайна на live-сообщениях (os.fork, 4 воркера).

ТИЛЬКИ ДЛЯ ОЦЕНКИ (не прод). Горячее место оффлайн-прогона — Tier-2
поверхностный fuzzy: каждое «мусорное» окно сканирует весь список алиасов
(~4k) через extractOne(WRatio) → ~7 с/сообщение.

Ускоритель (эквивалентность обеспечена конструкцией + parity-контролем):
  1. префильтр: WRatio >= 0.80 почти никогда не матчит фразу, у которой НИ
     ОДИН токен не делит 3-символьный префикс (lowercase) с токеном запроса.
     Окна без общих префиксов → None сразу (O(1) вместо ~40 мс);
  2. выжившие кандидаты (обычно 0–50) скорятся cdist (тот же WRatio, тот же
     score_cutoff), пара-победитель пересчитывается точным fuzz.WRatio
     (float64) — результат совместим с extractOne;
  3. несовпадающие списки фраз (тестовые индексы) → оригинальный путь;
  4. контроль: каждые SAMPLE_PARITY сообщений воркер пересчитывает сообщение
     С ОРИГИНАЛЬНОЙ _fuzzy_match и сравнивает итоговые сущности. Расхождение
     → аварийный выход (fail-closed), результаты не мержатся.

Запуск из корня проекта:
    .venv-refactor/bin/python scripts/eval_live_parallel.py [файл]
"""

import asyncio
import json
import os
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _name in ("parser", "processor"):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        _m.__path__ = [str(ROOT / _name)]
        sys.modules[_name] = _m

N_WORKERS = 4
SAMPLE_PARITY = 100  # каждое N-е сообщение в каждом воркере проверяется оригиналом
CACHE_MAX = 200_000


def _init_worker():
    """Дочерний процесс: собрать matcher и применить ускоритель Tier-2."""
    global _morph, _matcher, _orig_fuzzy, _surface, _surface_arr, _prefix_map, _cache
    from scripts.evaluate_live_messages import load_geo_and_stopwords
    from processor.morphology import Morphology
    from processor.phonetic_index import PhoneticIndex
    from processor.geo_matcher import GeoMatcher
    import processor.geo_matcher as gm

    _morph = Morphology()
    rows, name2id, stop = load_geo_and_stopwords()
    index = PhoneticIndex(_morph)
    index.build(rows)
    _matcher = GeoMatcher(_morph, index)
    _matcher._initialized = True
    _matcher._stopwords = stop

    _orig_fuzzy = gm._fuzzy_match
    _surface, _meta = _matcher._index.surface_phrases()

    import numpy as np
    import rapidfuzz.fuzz as fuzz
    from rapidfuzz.process import cdist

    _surface_arr = np.asarray(_surface, dtype=object)
    _prefix_map = {}
    for idx, ph in enumerate(_surface):
        for tok in ph.split():
            _prefix_map.setdefault(tok[:3].lower(), set()).add(idx)
    _cache = {}

    def fast_fuzzy(query, phrases, threshold):
        """Эквивалент _fuzzy_match: префильтр по префиксам + cdist + точный пересчёт."""
        if phrases is not _surface or len(phrases) != len(_surface):
            return _orig_fuzzy(query, phrases, threshold)
        key = (query, threshold)
        hit = _cache.get(key, False)
        if hit is not False:
            return hit
        idxs: set = set()
        for tok in query.split():
            idxs |= _prefix_map.get(tok[:3].lower(), set())
        result = None
        if idxs:
            order = sorted(idxs)
            cand_arr = _surface_arr[order]
            scores = cdist([query], cand_arr, scorer=fuzz.WRatio,
                           score_cutoff=threshold, workers=1)[0]
            j = int(scores.argmax())
            if scores[j] > 0:
                ph = cand_arr[j]
                exact = fuzz.WRatio(query, ph)  # точный float64 скор пары
                if exact >= threshold:
                    result = (ph, exact, order[j])
        if len(_cache) < CACHE_MAX:
            _cache[key] = result
        return result

    gm._fuzzy_match = fast_fuzzy


def _entity_key(ents):
    return [(e["geo_id"], round(e["score"], 6), e["matched_name"], e.get("source"))
            for e in ents]


def worker_main(worker_id: int, messages: list, out_path: str) -> None:
    _init_worker()
    from scripts.evaluate_live_messages import process_one
    import processor.geo_matcher as gm

    fast = gm._fuzzy_match
    n = len(messages)
    t0 = time.time()
    parity_done = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for j, (i, text) in enumerate(messages, 1):
            rec = asyncio.run(process_one(_matcher, _morph, i, text))

            # --- parity-проверка против оригинала ---
            if j % SAMPLE_PARITY == 0:
                gm._fuzzy_match = _orig_fuzzy
                try:
                    rec_orig = asyncio.run(process_one(_matcher, _morph, i, text))
                finally:
                    gm._fuzzy_match = fast
                parity_done += 1
                if _entity_key(rec["confident"] + rec["weak"]) != \
                        _entity_key(rec_orig["confident"] + rec_orig["weak"]):
                    print(f"[worker {worker_id}] PARITY FAIL on msg {i}:\n"
                          f"  fast={_entity_key(rec['confident'] + rec['weak'])}\n"
                          f"  orig={_entity_key(rec_orig['confident'] + rec_orig['weak'])}",
                          flush=True)
                    sys.exit(9)
                print(f"[worker {worker_id}] parity ok on msg {i}", flush=True)

            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if j % 200 == 0:
                rate = j / (time.time() - t0)
                print(f"[worker {worker_id}] {j}/{n} ({rate:.1f} msg/s)", flush=True)
    print(f"[worker {worker_id}] done: {n} msgs, {parity_done} parity checks, "
          f"{time.time() - t0:.1f}s", flush=True)


def main() -> None:
    from scripts.evaluate_live_messages import (
        load_messages, load_geo_and_stopwords, build_report, OUT_JSONL, OUT_JSON,
    )

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "live_messages.txt"
    messages = load_messages(path)
    indexed = list(enumerate(messages, 1))
    total = len(indexed)
    t0 = time.time()
    print(f"Loaded {total} messages; forking {N_WORKERS} workers", flush=True)

    shards = [indexed[k::N_WORKERS] for k in range(N_WORKERS)]
    parts = [str(ROOT / f"eval_results.part{k}.jsonl") for k in range(N_WORKERS)]
    pids = []
    for k in range(N_WORKERS):
        pid = os.fork()
        if pid == 0:
            try:
                worker_main(k, shards[k], parts[k])
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[worker {k}] FATAL: {e!r}", flush=True)
                os._exit(1)
            os._exit(0)
        pids.append(pid)

    failed = False
    for pid in pids:
        _, status = os.waitpid(pid, 0)
        if status != 0:
            failed = True
            print(f"worker pid={pid} exited with status {status}", flush=True)
    if failed:
        print("ABORT: parity or worker failure — results not merged", flush=True)
        sys.exit(1)

    results = []
    for part in parts:
        with open(part, encoding="utf-8") as f:
            for line in f:
                results.append(json.loads(line))
    results.sort(key=lambda r: r["i"])
    with open(OUT_JSONL, "w", encoding="utf-8") as out:
        for r in results:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")

    _, name2id, _ = load_geo_and_stopwords()
    report = build_report(results, str(path), name2id)
    report["runtime_s"] = round(time.time() - t0, 1)
    report["workers"] = N_WORKERS
    report["parity_checks"] = (total // N_WORKERS) // SAMPLE_PARITY * N_WORKERS
    report["accelerator"] = ("tier2 token-prefix prefilter + cdist (same WRatio/"
                             "cutoff, exact re-score) + cache; parity-checked fail-closed")
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    for part in parts:
        os.remove(part)

    summary = {k: v for k, v in report.items()
               if k not in ("weak_samples", "none_samples",
                            "fountanka_plain_samples", "top_matched_names")}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print("\nTop-30 matched names:")
    for m in report["top_matched_names"][:30]:
        print(f"  {m['count']:4d}  {m['name']}")
    print(f"\nDone in {report['runtime_s']}s -> {OUT_JSONL} / {OUT_JSON}")


if __name__ == "__main__":
    main()
