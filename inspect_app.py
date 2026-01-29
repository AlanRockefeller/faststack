from faststack.app import AppController
import inspect

methods = inspect.getmembers(AppController, predicate=inspect.isfunction)
print("Methods found:")
found = False
for name, _ in methods:
    if "auto_level" in name:
        print(f"  {name}")
        found = True

if not found:
    print("No auto_level methods found.")
