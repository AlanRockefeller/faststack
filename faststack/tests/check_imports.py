import importlib
import os
import sys
import traceback


def check_import(module_name: str) -> bool:
    """Try importing a module and print the result."""
    try:
        print(f"Importing {module_name}...")
        importlib.import_module(module_name)
        print(f"Success {module_name}")
        return True
    except ImportError as e:
        print(f"ImportError {module_name}: {e}")
        traceback.print_exc()
        return False
    except Exception as e:
        print(f"Non-ImportError during import of {module_name}: {e}")
        traceback.print_exc()
        return False


def main() -> None:
    # Add project root directory to path deterministically
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    sys.path.insert(0, project_root)

    failures = []
    for module in ["faststack.app", "faststack.tests.test_raw_pipeline"]:
        if not check_import(module):
            failures.append(module)

    if failures:
        print(f"\nFailed imports: {', '.join(failures)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
