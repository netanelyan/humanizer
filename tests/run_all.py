"""Run every test module without needing pytest:  py tests/run_all.py"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

MODULES = ("test_humanizer", "test_hebrew", "test_web", "test_webui", "test_parity")


def main():
    failures = 0
    for name in MODULES:
        print("\n\033[1m%s\033[0m" % name if sys.stdout.isatty() else "\n%s" % name)
        module = __import__(name)
        failures += module._run_all()
    print("\n%s" % ("ALL GREEN" if not failures else "%d module(s) failed" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
