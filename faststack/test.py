import os
from turbojpeg import TurboJPEG
print("TURBOJPEG =", os.environ.get("TURBOJPEG"))
jpeg = TurboJPEG(lib_path=os.environ.get("TURBOJPEG"))
print("TurboJPEG loaded OK")