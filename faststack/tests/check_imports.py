import sys
import os
import traceback

# Add current directory to path
sys.path.append(os.getcwd())

try:
    print("Importing faststack.app...")
    import faststack.app

    print("Success faststack.app")
except ImportError as e:
    print(f"ImportError faststack.app: {e}")

    traceback.print_exc()
except Exception as e:
    print(f"Non-ImportError during import of faststack.app: {e}")

    traceback.print_exc()

try:
    print("Importing faststack.tests.test_raw_pipeline...")
    import faststack.tests.test_raw_pipeline

    print("Success test_raw_pipeline")
except ImportError as e:
    print(f"ImportError test_raw_pipeline: {e}")

    traceback.print_exc()
except Exception as e:
    print(f"Non-ImportError during import of test_raw_pipeline: {e}")

    traceback.print_exc()
