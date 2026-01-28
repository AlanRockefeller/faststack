import sys
import os

# Add current directory to path
sys.path.append(os.getcwd())

try:
    print("Importing faststack.app...")
    from faststack.app import AppController
    print("Success faststack.app")
except Exception as e:
    print(f"Failed faststack.app: {e}")
    import traceback
    traceback.print_exc()

try:
    print("Importing faststack.tests.test_raw_pipeline...")
    import faststack.tests.test_raw_pipeline
    print("Success test_raw_pipeline")
except Exception as e:
    print(f"Failed test_raw_pipeline: {e}")
    import traceback
    traceback.print_exc()
