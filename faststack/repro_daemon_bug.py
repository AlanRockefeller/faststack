import concurrent.futures
import threading
import time


def check_daemon():
    print(
        f"Thread {threading.current_thread().name} daemon: {threading.current_thread().daemon}"
    )


def test_failure_mimic():
    print("Main thread daemon:", threading.current_thread().daemon)
    executor_container = {}

    def creator():
        executor_container["executor"] = concurrent.futures.ThreadPoolExecutor(
            max_workers=1
        )

    t = threading.Thread(target=creator, name="CreatorThread")
    t.daemon = True
    t.start()
    t.join()  # Creator thread dies

    executor = executor_container["executor"]
    # If the executor spawns worker threads when submit is called,
    # it might inherit from the CURRENT thread (main) instead of the creator thread.
    executor.submit(check_daemon).result()


if __name__ == "__main__":
    test_failure_mimic()
