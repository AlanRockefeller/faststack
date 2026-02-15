import threading
from concurrent.futures import ThreadPoolExecutor

def set_daemon():
    try:
        threading.current_thread().daemon = True
        print(f"Set daemon for {threading.current_thread().name}")
    except Exception as e:
        print(f"Failed to set daemon for {threading.current_thread().name}: {e}")

def check_daemon():
    return threading.current_thread().daemon

with ThreadPoolExecutor(max_workers=1, initializer=set_daemon) as executor:
    print(f"Result: {executor.submit(check_daemon).result()}")
