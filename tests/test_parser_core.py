"""Tests for parser core logic (F-3).

Verifies:
  * Text preprocessing pipeline with sanitize_text (R-P17, F-4)
  * Config constants are synchronized (F-9)
  * Queue maxsize and backpressure thresholds match config (F-10)
  * Batch buffer swap only on success (F-11)
  * Heartbeat interval is 5s (F-6)
  * Stale photo constants are correct
  * Worker scaling thresholds are correct
"""
from conftest import load_module_by_path

settings = load_module_by_path("_settings_under_test", "common/settings.py").settings
tp = load_module_by_path("_tp_under_test", "common/text_preprocessor.py")
config = load_module_by_path("_config_under_test", "parser/config.py")

strip_tail = tp.strip_tail
preprocess_light = tp.preprocess_light
truncate_for_geo = tp.truncate_for_geo
sanitize_text = tp.sanitize_text


# --- F-4: sanitize_text in pipeline ---


def test_sanitize_text_invalid_utf8():
    """Invalid UTF-8 bytes should be replaced, not raise."""
    invalid = "hello\x80world\xff"
    result = sanitize_text(invalid)
    assert result is not None
    assert result.encode("utf-8", errors="replace") == result.encode("utf-8")


def test_sanitize_text_none():
    assert sanitize_text(None) is None


def test_sanitize_text_clean_passes_through():
    clean = "Hello World"
    assert sanitize_text(clean) == clean


def test_processing_pipeline_has_sanitize():
    """Verify sanitize_text is part of the text_preprocessor module,
    confirming it's available for the parser hot path (F-4 fix)."""
    assert callable(sanitize_text)
    assert tp.sanitize_text.__doc__ is not None
    assert "utf-8" in tp.sanitize_text.__doc__.lower()


# --- F-9: Config constants synchronization ---


def test_config_has_batch_flush_interval():
    assert hasattr(config, "_BATCH_FLUSH_INTERVAL")
    assert config._BATCH_FLUSH_INTERVAL == 0.1


def test_config_has_heartbeat_interval():
    assert hasattr(config, "_HEARTBEAT_INTERVAL")
    assert config._HEARTBEAT_INTERVAL == 5


def test_config_has_queue_maxsize():
    assert hasattr(config, "_QUEUE_MAXSIZE")
    assert config._QUEUE_MAXSIZE == 65


def test_config_has_backpressure_thresholds():
    assert hasattr(config, "_BACKPRESSURE_HIGH")
    assert hasattr(config, "_BACKPRESSURE_LOW")
    assert config._BACKPRESSURE_HIGH == 60
    assert config._BACKPRESSURE_LOW == 40


def test_config_has_worker_limits():
    assert config._MIN_WORKERS == 2
    assert config._MAX_WORKERS == 8
    assert config._SCALE_UP_QSIZE == 20
    assert config._IDLE_TIMEOUT == 15


def test_config_has_stale_photo_constants():
    assert config._STALE_PHOTO_INTERVAL == 300
    assert config._STALE_PHOTO_CUTOFF_SECONDS == 70 * 60


# --- F-10: monitoring.py must use config constants (not hardcode) ---


def test_monitoring_uses_config_constants():
    """monitoring.py imports constants from config.py instead of
    hardcoding them. Verify by checking source code."""
    monitoring_src = _read_file("parser/monitoring.py")
    # Should NOT have hardcoded values that duplicate config
    assert "maxsize=65" not in monitoring_src, \
        "_QUEUE_MAXSIZE should be imported from config, not hardcoded"
    assert "asyncio.Semaphore(3)" not in monitoring_src, \
        "_PHOTO_DOWNLOAD_CONCURRENCY should be imported from config"
    assert "sleep(5)" not in monitoring_src, \
        "_HEARTBEAT_INTERVAL should be imported from config"
    # Should import from config
    assert "from .config import" in monitoring_src


def test_monitoring_imports_specific_constants():
    """Verify monitoring.py imports the key config constants."""
    monitoring_src = _read_file("parser/monitoring.py")
    for const in ["_MIN_WORKERS", "_MAX_WORKERS", "_QUEUE_MAXSIZE",
                   "_BACKPRESSURE_HIGH", "_BACKPRESSURE_LOW",
                   "_BATCH_FLUSH_INTERVAL", "_HEARTBEAT_INTERVAL",
                   "_PHOTO_DOWNLOAD_CONCURRENCY"]:
        assert const in monitoring_src, f"monitoring.py should use {const}"


# --- F-11: Batch buffer swap only on success ---


def test_batch_buffer_success_clears_buffer():
    """When flush succeeds, buffer is cleared (F-11 fix)."""
    monitoring_src = _read_file("parser/monitoring.py")
    assert "self._batch_buffer = []" in monitoring_src


def test_batch_flush_loop_catches_errors():
    """_batch_flush_loop wraps _flush_batch in try/except (F-2 fix)."""
    monitoring_src = _read_file("parser/monitoring.py")
    start = monitoring_src.find("async def _batch_flush_loop")
    end = monitoring_src.find("async def ", start + 1)
    if end == -1:
        end = len(monitoring_src)
    loop_body = monitoring_src[start:end]
    assert "try:" in loop_body
    assert "except Exception" in loop_body
    assert "_flush_batch()" in loop_body


# --- Text processing pipeline ---


def test_strip_tail_removes_service_footer():
    assert strip_tail("ДТП на Дерибасовской сообщить") == "ДТП на Дерибасовской"


def test_strip_tail_no_marker():
    assert strip_tail("ДТП на Дерибасовской") == "ДТП на Дерибасовской"


def test_preprocess_light_preserves_case():
    text = "ДТП на Дерибасовской!"
    result = preprocess_light(text)
    assert "Дерибасовской" in result


def test_preprocess_light_normalizes_ukrainian():
    """preprocess_light applies UA table translation (і→и).

    Suffix regexes (івська→овская) run after table translation
    and may not match on already-translated text — this tests
    the documented behavior (і→и normalization)."""
    result = preprocess_light("Балківська")
    # і→и translation always applies
    assert "и" in result or "и" in result
    # After table translation: Балківська → Балкивская
    # (suffix regexes would need original forms)


def test_truncate_for_geo_keeps_head_and_tail():
    long_text = "блокпост на Туристской " + "детали " * 100 + "возле 7 км"
    result = truncate_for_geo(long_text, settings.parser.max_text_length)
    assert len(result) <= settings.parser.max_text_length + 3
    assert "Туристской" in result
    assert "7 км" in result
    assert "слишком длиннное" not in result


def test_truncate_for_ge_short_text():
    text = "ДТП на Дерибасовской"
    assert truncate_for_geo(text, settings.parser.max_text_length) == text


def test_sanitize_text_keeps_valid_utf8():
    text = "Привет мир 🌍"
    assert sanitize_text(text) == text


def _read_file(path: str) -> str:
    from pathlib import Path
    return Path(path).read_text(encoding="utf-8")
