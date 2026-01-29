import os
import sys
import traceback

# Add project root to path
# tests/ is at project_root/tests, so project_root is ..
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
print(f"Sys Path[0]: {sys.path[0]}")

try:
    print("Attempting import faststack.app...")
    import faststack.app as app

    print(f"Import faststack.app success! ({app.__file__})")

    print("Attempting import AppController...")
    from faststack.app import AppController

    print("Import AppController success!")

    print("Attributes:")
    print(f"get_active_edit_path: {hasattr(AppController, 'get_active_edit_path')}")
except Exception:
    print("Import FAILED:")
    traceback.print_exc()
    raise  # optional: makes CI fail loud
