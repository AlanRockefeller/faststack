import sys
import os
import traceback

# Add project root to path
# We are running from faststack/faststack, so root is ..
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
print(f"Sys Path: {sys.path[0]}")

try:
    print("Attempting import faststack.app...")
    import faststack.app
    print("Import faststack.app success!")
    
    print("Attempting import AppController...")
    from faststack.app import AppController
    print("Import AppController success!")
    
    print("Attributes:")
    print(f"get_active_edit_path: {hasattr(AppController, 'get_active_edit_path')}")
except Exception:
    print("Import FAILED:")
    traceback.print_exc()
