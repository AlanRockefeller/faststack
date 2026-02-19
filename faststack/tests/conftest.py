# faststack/tests/conftest.py
import faulthandler
import os
import signal
import sys


def _dump_usr2(signum, frame):
    sys.stderr.write(f"\n\n=== SIGUSR2: pid={os.getpid()} ===\n")
    sys.stderr.flush()
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    sys.stderr.write("=== end SIGUSR2 dump ===\n\n")
    sys.stderr.flush()


def pytest_configure(config):
    # Enable faulthandler for crashes too
    faulthandler.enable(all_threads=True)

    # Install a *non-terminating* handler if signal available (Unix only)
    if hasattr(signal, "SIGUSR2"):
        signal.signal(signal.SIGUSR2, _dump_usr2)
