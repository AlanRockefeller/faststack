import sys
import importlib.resources


def check_imports():
    print("Checking imports...")
    try:
        import faststack
        import faststack.ui
        import faststack.io
        import faststack.imaging
        import faststack.app

        print("  [OK] Imports successful")
    except ImportError as e:
        print(f"  [FAIL] Import failed: {e}")
        return False
    return True


def check_cli():
    print("Checking CLI entry point...")
    try:
        from faststack.app import cli

        if not callable(cli):
            print("  [FAIL] faststack.app.cli is not callable")
            return False
        print("  [OK] faststack.app.cli found")
    except ImportError:
        print("  [FAIL] Could not import faststack.app.cli")
        return False
    except Exception as e:
        print(f"  [FAIL] Error checking CLI: {e}")
        return False
    return True


def check_assets():
    print("Checking assets (QML files)...")
    try:
        # For Python 3.9+ standard library importlib.resources
        # We look for any .qml file in faststack package
        qml_files = list(importlib.resources.files("faststack").rglob("*.qml"))
        count = len(qml_files)
        if count > 0:
            print(f"  [OK] Found {count} QML files")
            for p in qml_files[:3]:
                print(f"    - {p.name}")
        else:
            print("  [FAIL] No QML files found in package resources!")
            print(
                "         (Did you include package_data in pyproject.toml / MANIFEST.in?)"
            )
            return False
    except Exception as e:
        print(f"  [FAIL] Asset check failed: {e}")
        return False
    return True


def main():
    print("=== FastStack Smoke Verification ===")
    print(f"Python: {sys.version}")

    if not check_imports():
        sys.exit(1)

    if not check_cli():
        sys.exit(1)

    if not check_assets():
        sys.exit(1)

    print("\n[SUCCESS] faststack package seems healthy.")


if __name__ == "__main__":
    main()
