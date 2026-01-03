
import sys
import os
from pathlib import Path
import configparser

# Update sys.path to include the project root
sys.path.append(r"c:\code\faststack")

# Mock logging setup to avoid creating real logs/directories
import faststack.logging_setup
import faststack.config

def test_config_persistence():
    print("Testing config persistence...")
    
    # Use a temporary file for testing
    test_config_dir = Path("c:/code/faststack/test_config_dir")
    test_config_dir.mkdir(exist_ok=True)
    
    # Monkeypatch get_app_data_dir to use local dir
    faststack.config.get_app_data_dir = lambda: test_config_dir
    
    # 1. Initialize config (should create defaults)
    app_config = faststack.config.AppConfig()
    print(f"Config path: {app_config.config_path}")
    
    # Verify default
    initial_val = app_config.get('core', 'auto_level_threshold')
    print(f"Initial value: {initial_val}")
    if initial_val != "0.1":
        print("FAIL: Default value unexpected")
    
    # 2. Modify value
    new_val = "0.05"
    print(f"Setting value to: {new_val}")
    app_config.set('core', 'auto_level_threshold', new_val)
    app_config.save()
    
    # 3. Reload config from disk directly to verify file content
    raw_config = configparser.ConfigParser()
    raw_config.read(app_config.config_path)
    file_val = raw_config.get('core', 'auto_level_threshold')
    print(f"Value in file: {file_val}")

    # 4. Re-initialize AppConfig (simulate app restart)
    # We must clear the global instance or create a new one to force reload
    # AppConfig.__init__ calls self.load()
    app_config_2 = faststack.config.AppConfig()
    loaded_val = app_config_2.get('core', 'auto_level_threshold')
    print(f"Loaded value: {loaded_val}")
    
    if loaded_val == new_val:
        print("SUCCESS: Value persisted correctly")
    else:
        print(f"FAIL: Value did not persist. Got {loaded_val}, expected {new_val}")
        
    # Clean up
    if (test_config_dir / "faststack.ini").exists():
        (test_config_dir / "faststack.ini").unlink()
    test_config_dir.rmdir()

if __name__ == "__main__":
    test_config_persistence()
