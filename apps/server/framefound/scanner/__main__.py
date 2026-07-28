"""Scanner service entrypoint (skeleton).

TODO(m2): initialize watchers per enabled library, schedule reconciliation
scans, and enqueue stable new/changed files. For Milestone 0 this only proves
the container wiring.
"""

import time

import structlog

log = structlog.get_logger()

if __name__ == "__main__":
    log.info("scanner.skeleton_started", note="Milestone 2 implements scanning")
    while True:  # keep the container alive so compose health-wiring can be tested
        time.sleep(3600)
