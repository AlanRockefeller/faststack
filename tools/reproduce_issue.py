
import logging
import sys

log = logging.getLogger(__name__)

# Mock the parts of the app needed for setup_logging
# We need to make sure we can import faststack modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from faststack.logging_setup import setup_logging

def test_logging(debug_mode):
    print(f"\n--- Testing with debug={debug_mode} ---")
    # Reset logging
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
        h.close()
    
    setup_logging(debug=debug_mode)
    
    logger = logging.getLogger("test_logger")
    
    # We want to capture stderr/stdout to check if it printed
    # But for a simple script run by the agent, just seeing the output is enough
    # or we can check the effective level
    
    effective_level = logger.getEffectiveLevel()
    print(f"Effective level: {logging.getLevelName(effective_level)}")
    
    if logger.isEnabledFor(logging.INFO):
        print("INFO logs are ENABLED")
    else:
        print("INFO logs are DISABLED")

if __name__ == "__main__":
    print("Reproduction Script Starting...")
    test_logging(debug_mode=False)
    test_logging(debug_mode=True)
