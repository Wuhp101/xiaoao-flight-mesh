from __future__ import annotations

import os
import time
import traceback

from .workflow import main as run_one_job_page


def main() -> None:
    os.environ.setdefault(
        "FLIGHT_MESH_JOB_URL",
        "https://life-tg.wuhp101.workers.dev/api/flight-scan-job?claim=1",
    )
    idle_seconds = max(5, min(120, int(os.getenv("FLIGHT_MESH_BRIDGE_IDLE_SECONDS", "15"))))
    busy_seconds = max(1, min(30, int(os.getenv("FLIGHT_MESH_BRIDGE_BUSY_SECONDS", "2"))))
    print(f"xiaoao bridge polling {os.environ['FLIGHT_MESH_JOB_URL']}", flush=True)
    while True:
        started = time.monotonic()
        try:
            run_one_job_page()
            elapsed = time.monotonic() - started
            # A real page normally takes longer than an idle claim. Keep draining
            # a live full-matrix job quickly, but back off when no job is queued.
            time.sleep(busy_seconds if elapsed >= 1.0 else idle_seconds)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            print(f"bridge iteration failed: {type(error).__name__}: {error}", flush=True)
            if os.getenv("FLIGHT_MESH_BRIDGE_TRACEBACK") == "1":
                traceback.print_exc()
            time.sleep(idle_seconds)


if __name__ == "__main__":
    main()
