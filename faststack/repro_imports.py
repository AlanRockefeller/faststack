try:
    from unittest.mock import MagicMock

    print("Success: from unittest.mock import MagicMock")
except ImportError as e:
    print(f"Failed: {e}")

try:
    import faststack.app

    print("Success: import faststack.app")
except ImportError as e:
    print(f"Failed: import faststack.app: {e}")
except Exception as e:
    print(f"Failed: import faststack.app error: {e}")
