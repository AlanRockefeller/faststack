"""Debug script to run tests and capture full output."""

import os

# Change to faststack directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Run the test
import unittest

loader = unittest.TestLoader()
suite = loader.discover(".", pattern="test_editor_loading.py")
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)

# Print summary
print(f"\n\nTests run: {result.testsRun}")
print(f"Failures: {len(result.failures)}")
print(f"Errors: {len(result.errors)}")

for test, traceback in result.failures:
    print(f"\nFAILURE: {test}")
    print(traceback)

for test, traceback in result.errors:
    print(f"\nERROR: {test}")
    print(traceback)
