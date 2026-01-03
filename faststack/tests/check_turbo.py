
try:
    import turbojpeg
    print("turbojpeg module found")
    print(f"Dir: {dir(turbojpeg)}")
    if hasattr(turbojpeg, 'TJFLAG_FASTDCT'):
        print(f"TJFLAG_FASTDCT: {turbojpeg.TJFLAG_FASTDCT}")
    else:
        print("TJFLAG_FASTDCT not found in module")
except ImportError:
    print("turbojpeg module not found")
