#!/usr/bin/env python3
"""Run test_streets_data.py without pytest installed."""

import sys
import types

# Minimal pytest stub — the test file only uses plain asserts, no fixtures/markers.
pytest_stub = types.ModuleType("pytest")


def _no_marker(*args, **kwargs):
    return lambda func: func


pytest_stub.mark = types.SimpleNamespace(parametrize=lambda *a, **k: (lambda f: f))
pytest_stub.fixture = _no_marker
sys.modules["pytest"] = pytest_stub

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tests"))

import test_streets_data as t

results = []
for name in dir(t):
    if name.startswith("test_"):
        fn = getattr(t, name)
        try:
            fn()
            results.append((name, "PASS"))
        except Exception as e:
            results.append((name, f"FAIL: {e}"))

for name, status in results:
    print(f"  {status:8s}  {name}")
print(f"\n{len(results)} tests, {sum(1 for _, s in results if s == 'PASS')} passed")
