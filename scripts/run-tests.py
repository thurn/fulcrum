"""Discover the whole suite and report timings without external services."""

import importlib
import inspect
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings = []

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        self.timings.append((time.monotonic() - self.started, test.id()))
        super().stopTest(test)


def main() -> int:
    class_suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), top_level_dir=str(ROOT)
    )
    function_suite = unittest.TestSuite()
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        module = importlib.import_module(f"tests.{path.stem}")
        for name, function in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("test_") and function.__module__ == module.__name__:
                function_suite.addTest(unittest.FunctionTestCase(function))
    suite = unittest.TestSuite((class_suite, function_suite))
    result = unittest.TextTestRunner(resultclass=TimedResult).run(suite)
    print("\nSlowest tests:")
    for seconds, name in sorted(result.timings, reverse=True)[:5]:
        print(f"  {seconds:.3f}s {name}")
    if result.testsRun == 0 or result.skipped:
        print(
            "Empty or skipped tests are not a passing complete check.", file=sys.stderr
        )
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
