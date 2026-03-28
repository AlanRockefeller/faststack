import importlib
import os
import sys
import traceback


def check_import(module_name: str) -> None:
    """Try importing a module and print the result."""
    try:
        print(f"Importing {module_name}...")
        importlib.import_module(module_name)
        print(f"Success {module_name}")
    except ImportError as e:
        print(f"ImportError {module_name}: {e}")
        traceback.print_exc()
    except Exception as e:
        print(f"Non-ImportError during import of {module_name}: {e}")
        traceback.print_exc()


def main() -> None:
    # Add current directory to path
    sys.path.append(os.getcwd())

    check_import("faststack.app")
    check_import("faststack.tests.test_raw_pipeline")


if __name__ == "__main__":
    main()
