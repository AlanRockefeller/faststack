import sys
import os
from pathlib import Path

# Add the directory containing the 'faststack' package to the Python path
sys.path.insert(0, str(Path(__file__).parent / "faststack"))

# Now, try to run the module
import runpy
runpy.run_module('faststack.app', run_name='__main__', alter_sys=True)