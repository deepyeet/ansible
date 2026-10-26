"""Dependency-free test runner (pytest-style functions, no pytest
needed - the container images here are minimal).
Usage: python3 tests/run_tests.py"""
import importlib
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

tests = []
# Discover every test_*.py module in this directory - a hardcoded
# list silently skipped test_gates and test_merge once already.
for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
    mod_name = path.stem
    mod = importlib.import_module(mod_name)
    tests += [(f"{mod_name}::{n}", getattr(mod, n))
              for n in dir(mod) if n.startswith("test_")]
failed = 0
for name, fn in sorted(tests):
    try:
        fn()
        print(f"PASS {name}")
    except Exception:
        failed += 1
        print(f"FAIL {name}")
        traceback.print_exc()
print(f"\n{len(tests) - failed}/{len(tests)} passed")
sys.exit(1 if failed else 0)
