import concurrent.futures
import threading
import time


def check_daemon():
    print(
        f"Thread {threading.current_thread().name} daemon: {threading.current_thread().daemon}"
    )


def test_prespawn():
    print("Main thread daemon:", threading.current_thread().daemon)
    executor_container = {}
    max_workers = 4

    def creator():
        print(
            f"Creator thread {threading.current_thread().name} daemon: {threading.current_thread().daemon}"
        )
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        executor_container["executor"] = executor
        # Force spawn all workers while we are in this daemon thread
        # We need to submit at least 'max_workers' tasks and wait for them to be
        # picked up by separate threads.
        futures = [executor.submit(time.sleep, 0.05) for _ in range(max_workers)]
        concurrent.futures.wait(futures)
        print("All workers spawned from daemon thread.")

    t = threading.Thread(target=creator, name="CreatorThread")
    t.daemon = True
    t.start()
    t.join()

    executor = executor_container["executor"]
    print("Main thread calling submit (which should reuse a daemon worker)...")
    executor.submit(check_daemon).result()


if __name__ == "__main__":
    test_prespawn()
