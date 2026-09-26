"""Тесты миграции 17-geo-sync.sql и консистентности postgres/data/geo.csv.

Покрытие (N1/N2 подплана улучшения NLP):
  1. geo.csv парсится целиком, новые объекты присутствуют, координаты правдоподобны,
     алиасы не дублируются между строками (иначе merge-логика молча теряет строку).
  2. Миграция содержит все идемпотентные механизмы (anti-join вставка, merge
     алиасов по канонику, backfill geom_m, pg_notify geo_updated, BEGIN/COMMIT).
  3. Python-зеркало merge-алгоритма: повторный прогон csv по «БД», засеянной
     тем же csv, не вставляет ничего и не меняет алиасы (идемпотентность).

SQL-миграция без живого Postgres проверяется статически (как в
test_verifier_checks.py); end-to-end прогон — на деплое:
  docker compose exec -T postgres psql -U postgres -d postgres \
      -f /docker-entrypoint-initdb.d/17-geo-sync.sql
"""

import csv
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
csv.field_size_limit(10_000_000)

GEO_CSV = ROOT / "postgres/data/geo.csv"
MIGRATION = ROOT / "postgres/init-scripts/17-geo-sync.sql"

NEW_OBJECTS = [
    ("БКМ", {"БКМ", "Беляевская картонная мануфактура", "бкм"}),
    ("улица Бабеля", {"улица Бабеля", "Бабеля"}),
    ("улица Ждахи", {"улица Ждахи", "Ждахи"}),
    ("Сергея Шелухина", {"Сергея Шелухина", "улица Сергея Шелухина", "Шелухина"}),
    ("Дача Дашкевича", {"Дача Дашкевича", "Дашкевича"}),
    # Живой поток (live_messages.txt): народные имена без которых сообщения
    # не геолоцировались («6 елемент», «Балтская дорога ТЦК»).
    ("Шестой элемент", {"Шестой элемент", "6 км", "6км", "6 элемент",
                        "Елемент", "Елимент", "шкодогорка"}),
    ("Куликово", {"Куликово", "Куликовом", "Куликовский",
                  "2 куликовский", "2й куликовский", "2-й куликовский"}),
    ("Балтская дорога", {"Балтская дорога", "Балтская"}),
    # export7 (2026-09-25): «Гребной/Грибной канал», «Орион блокпост»
    # (ресторан на Житомирской, не промзона), «Окружная. ОККО» (Нерубайское).
    ("Гребной канал", {"Гребной канал", "Гребний канал", "Грибной канал"}),
    ("Орион", {"Орион", "Орион Житомирская", "ресторан Орион"}),
    ("Окружная дорога", {"Окружная дорога", "Окружная", "Окружна вулиця"}),
]

# Алиасы к СУЩЕСТВУЮЩИМ объектам (каноник уже в csv/БД; миграция дозаполняет
# их секцией 3). Великодолинское: народное имя Акаржа (live-поток); до этого
# сообщения «Акаржа провулок» не геолоцировались вовсе.
NEW_ALIASES_EXISTING = {
    "Великодолинское": {"Акаржа", "Аккаржа", "Акарж"},
    "9 Фонтана": {"бык"},  # сквер с быком у 9-й станции — главный ориентир потока
    # export7: «Аркадиевский переулок» (0.87 на грани / пин падал на фоллбек),
    # «застава 2» (реверс порядка слов — stem_reorder 0.78 < 0.80),
    # «М. Долиной»/«Новой долины» (род. падеж направления), «При Лиманским».
    "Аркадийский": {"Аркадиевский", "Аркадьевский"},
    "2 застава": {"застава 2"},
    "Новая Долина": {"М долина", "Новой долины"},
    "Прилиманское": {"При лиманское"},
}

WKT_PREFIXES = ("POINT", "LINESTRING", "POLYGON", "MULTIPOINT",
                "MULTILINESTRING", "MULTIPOLYGON", "GEOMETRYCOLLECTION")


def _load_csv():
    rows = []
    with open(GEO_CSV, encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        header = next(rd)
        assert header[:3] == ["names", "wkt_geom", "type"], header
        for r in rd:
            if not r or not r[0].strip():
                continue
            assert len(r) >= 3, f"csv row needs 3 fields: {r!r}"
            rows.append({
                "names": r[0].split("|"),
                "wkt": r[1].strip(),
                "type": r[2].strip(),
            })
    return rows


# ===========================================================================
# 1. Консистентность geo.csv
# ===========================================================================

def test_csv_parses_fully():
    rows = _load_csv()
    assert len(rows) >= 1110, "geo.csv неожиданно потерял строки"


def test_csv_new_objects_present_with_aliases():
    rows = _load_csv()
    by_canon = {r["names"][0]: r for r in rows}
    for canon, expected_aliases in NEW_OBJECTS:
        assert canon in by_canon, f"{canon} отсутствует в geo.csv"
        assert set(by_canon[canon]["names"]) == expected_aliases


@pytest.mark.parametrize("canon", ["БКМ", "улица Бабеля", "улица Ждахи",
                                   "Сергея Шелухина", "Дача Дашкевича",
                                   "Шестой элемент", "Куликово",
                                   "Балтская дорога", "Гребной канал",
                                   "Орион", "Окружная дорога"])
def test_csv_new_object_coords_plausible(canon):
    """Координаты новых объектов в разумных границах Одесской агломерации."""
    rows = _load_csv()
    row = next(r for r in rows if r["names"][0] == canon)
    assert row["wkt"].upper().startswith(WKT_PREFIXES), row["wkt"][:40]
    nums = [float(x) for x in re.findall(r"-?\d+\.\d+", row["wkt"])]
    assert nums, f"нет координат в WKT: {row['wkt'][:60]}"
    lons, lats = nums[0::2], nums[1::2]
    assert all(28.0 <= lon <= 34.0 for lon in lons), f"{canon}: lon вне региона"
    assert all(44.0 <= lat <= 48.5 for lat in lats), f"{canon}: lat вне региона"


def test_csv_new_aliases_added_to_existing():
    """Народные алиасы дозаполнены к существующим строкам (не отдельными
    строками — иначе merge вставил бы только первую, а вторая потерялась)."""
    rows = _load_csv()
    names_by_canon = {}
    for r in rows:
        names_by_canon.setdefault(r["names"][0], []).append(r["names"])
    for canon, aliases in NEW_ALIASES_EXISTING.items():
        assert canon in names_by_canon, f"{canon} отсутствует в geo.csv"
        union = {a for names in names_by_canon[canon] for a in names}
        missing = aliases - union
        assert not missing, f"алиасы {missing} не добавлены к {canon}"


def test_csv_no_cross_row_alias_duplicates():
    """Алиас не должен встречаться в двух строках: merge-логика вставляет
    только первую строку по rn, вторая молча теряется.

    БАЗА: в csv до-существующая грязь (19 кросс-строчных алиасов: «вокзал»,
    «Усатово» у «Нерубайское» и т.п. — на живых томах эти строки уже есть,
    анти-джойн их находит, потери нет). Тест — tripwire: число дублей не
    должно РАСТИ; новые объекты обязаны быть чистыми (следующий тест).
    """
    rows = _load_csv()
    new_canons = {c for c, _ in NEW_OBJECTS}
    seen = {}
    dupes = []
    for r in rows:
        for alias in r["names"]:
            alias = alias.strip()
            if not alias:
                continue
            if alias in seen and seen[alias] != r["names"][0]:
                dupes.append((alias, seen[alias], r["names"][0]))
            seen.setdefault(alias, r["names"][0])
    # Новые объекты не добавили ни одного нового дубля
    fresh = [d for d in dupes
             if d[1] in new_canons or d[2] in new_canons]
    assert not fresh, f"новые объекты создали дубли алиасов: {fresh}"
    assert len(dupes) <= 19, (
        f"число кросс-строчных дублей выросло ({len(dupes)} > 19): "
        f"merge вставит только первую строку — проверьте список: {dupes[:5]}"
    )


def test_csv_types_in_known_set():
    """type из csv — известные значения схемы.

    'infrastructure' (×11) — до-существующее отклонение от списка в
    10-type-config.sql (там geo_type_descriptions его не описывает); учтено
    здесь как наблюдаемая реальность, кандидат на чистку справочника.
    """
    rows = _load_csv()
    known = {"street", "village", "town", "station", "park", "landmark",
             "market", "square", "bridge", "embankment", "district",
             "stop", "beach", "forest", "water",
             "infrastructure"}  # до-существующее в данных (см. выше)
    bad = {r["type"] for r in rows if r["type"] not in known}
    assert not bad, f"неизвестные type: {bad}"


# ===========================================================================
# 2. Статические проверки миграции (без живого Postgres)
# ===========================================================================

@pytest.fixture(scope="module")
def migration_sql():
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_wrapped_in_transaction(migration_sql):
    assert re.search(r"^BEGIN;", migration_sql, re.M)
    assert re.search(r"^COMMIT;", migration_sql, re.M)


def test_migration_uses_copy_from_image_path(migration_sql):
    assert "/docker-entrypoint-initdb.d/data/geo.csv" in migration_sql


def test_migration_insert_is_anti_join_by_any_alias(migration_sql):
    """Вставка только строк без пересечения алиасов с БД (&&) — ключ
    идемпотентности: повторный прогон не создаёт дублей."""
    assert re.search(r"NOT EXISTS\s*\(SELECT 1 FROM geo g WHERE g\.names && ",
                     migration_sql)
    assert "q.rn < p.rn" in migration_sql, "нет дедупа csv-строк между собой"


def test_migration_merges_aliases_by_canonical(migration_sql):
    assert "g.names[1] = p.names_arr[1]" in migration_sql
    assert "array_cat" in migration_sql


def test_migration_heals_geometry_and_backfills_geom_m(migration_sql):
    assert "SET geom = heal.geom" in migration_sql
    assert "ST_Transform(ST_MakeValid(geom), 3857)" in migration_sql


def test_migration_notifies_geo_updated(migration_sql):
    assert "pg_notify('geo_updated'" in migration_sql


def test_migration_no_top_level_perform(migration_sql):
    """PERFORM вне DO-блока — синтаксическая ошибка на верхнем уровне psql
    (валиден только в plpgsql). Реальный инцидент: postgres exit 3 при
    инициализации тома — вся БД не поднималась с первого раза.
    Единственная разрешённая форма — внутри DO $$ ... $$."""
    assert not re.search(r"^PERFORM\b", migration_sql, re.M), (
        "PERFORM на верхнем уровне .sql недопустим — обернуть в DO $$ ... $$"
    )
    # и notify-страховка именно внутри DO-блока:
    assert re.search(r"DO \$\$\s*BEGIN\s+PERFORM pg_notify", migration_sql)


def test_migration_defines_safe_geom(migration_sql):
    assert "CREATE OR REPLACE FUNCTION safe_geom_from_text" in migration_sql


def test_migration_verifies_new_objects(migration_sql):
    """Блок отчёта перечисляет все новые каноники и warns о пропавших."""
    for canon, _ in NEW_OBJECTS:
        assert canon in migration_sql, canon


def test_migration_syncs_existing_object_type(migration_sql):
    """Секция 3b: тип существующих объектов синхронизируется по csv (раньше
    merge трогал только алиасы — Ильичевск-street при POLYGON не исправлялся)."""
    assert "SET type = tf.new_type" in migration_sql
    assert "g.type IS DISTINCT FROM p.type" in migration_sql


def test_csv_illichevsk_is_town():
    """Город Ильичевск (POLYGON) должен быть town: тип street ломал
    приоритизацию кандидатов в process_candidates_v2 и сортировку в матчере."""
    rows = _load_csv()
    row = next(r for r in rows if r["names"][0] == "Ильичевск")
    assert row["type"] == "town", row["type"]


def test_migration_drops_temp_table(migration_sql):
    assert "DROP TABLE temp_geo_sync" in migration_sql


# ===========================================================================
# 3. Python-зеркало merge-алгоритма: идемпотентность второго прогона
# ===========================================================================

def _mirror_sync(db, rows):
    """Зеркало SQL-логики миграции.

    db — список записей {'canon': str, 'aliases': set} (одна запись = одна
    строка geo; каноник хранится ЯВНО, как names[1] в SQL — не через порядок
    элементов множества, который в Python не определён).

    Возвращает (inserted_canons, merged_aliases):
      вставка — если ни один алиас не пересекается с БД (и не вставлен ранее
      в этом прогоне); merge — дозаполнение алиасов ВСЕМ db-строкам, чей
      каноник равен канонику csv-строки (JOIN по names[1]; при дублированных
      канониках объединяет алиасы всех таких строк за один проход).
    """
    inserted, merged = [], 0
    inserted_aliases = set()
    prep = [(r["names"], r["wkt"].upper().startswith(WKT_PREFIXES)) for r in rows]
    for names, has_geom in prep:
        alias_set = {a.strip() for a in names if a.strip()}
        if has_geom and not (alias_set & inserted_aliases) and not any(
            alias_set & obj["aliases"] for obj in db
        ):
            db.append({"canon": names[0], "aliases": alias_set})
            inserted_aliases |= alias_set
            inserted.append(names[0])
    for names, _ in prep:
        canon = names[0]
        csv_aliases = {a.strip() for a in names if a.strip()}
        for obj in db:
            if obj["canon"] == canon:
                new = csv_aliases - obj["aliases"]
                if new:
                    obj["aliases"] |= new
                    merged += len(new)
    return inserted, merged


def _seed_db(rows):
    """Состояние БД «после 04-load-data.sql»: каждая csv-строка — отдельный объект."""
    return [
        {"canon": r["names"][0],
         "aliases": {a.strip() for a in r["names"] if a.strip()}}
        for r in rows
    ]


def test_mirror_second_run_is_noop():
    """Сходимость: на засеянной из csv БД первый прогон может сливать алиасы
    дублированных каноников (объединение за один проход), но ВТОРОЙ прогон —
    строгий no-op: ничего не вставляется, алиасы не меняются."""
    rows = _load_csv()
    db = _seed_db(rows)
    _mirror_sync(db, rows)  # прогон 1 — сходит union дублированных каноников
    inserted, merged = _mirror_sync(db, rows)  # прогон 2
    assert inserted == [], "второй прогон не должен вставлять объекты"
    assert merged == 0, "второй прогон не должен менять алиасы"


def test_mirror_first_run_inserts_new_objects():
    """На БД без новых объектов миграция вставляет все новые объекты
    (полными алиасами) и после одного прохода достигает сходимости."""
    rows = _load_csv()
    new_canons = {c for c, _ in NEW_OBJECTS}
    old_rows = [r for r in rows if r["names"][0] not in new_canons]
    db = _seed_db(old_rows)
    inserted, _merged = _mirror_sync(db, rows)
    assert set(inserted) == new_canons
    # Новые объекты вставлены со всеми своими алиасами (merge им не нужен):
    by_canon = {obj["canon"]: obj for obj in db}
    for canon, expected in NEW_OBJECTS:
        assert by_canon[canon]["aliases"] == expected
    # Идемпотентность: повторный прогон — no-op
    assert _mirror_sync(db, rows) == ([], 0)
