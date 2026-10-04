from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("relaycore.supervisor")


class WorkerSupervisor:
    def __init__(self, count: int):
        self.count = count
        self.processes: dict[str, subprocess.Popen] = {}
        self.stopped: set[str] = set()
        self.lock = threading.Lock()
        self.monitor_stop = threading.Event()
        self.monitor: threading.Thread | None = None

    def start(self) -> None:
        for index in range(1, self.count + 1):
            self.start_worker(f"worker-{index}")
        self.monitor = threading.Thread(target=self._monitor, daemon=True, name="worker-supervisor")
        self.monitor.start()

    def start_worker(self, worker_id: str) -> int:
        with self.lock:
            if worker_id in self.processes and self.processes[worker_id].poll() is None:
                return self.processes[worker_id].pid
            if worker_id not in {f"worker-{i}" for i in range(1, self.count + 1)}:
                raise ValueError("Unknown worker id")
            env = os.environ.copy()
            env["RELAYCORE_WORKER_ID"] = worker_id
            root = Path(__file__).resolve().parent.parent
            process = subprocess.Popen(
                [sys.executable, "-m", "app.worker"], cwd=root, env=env,
                stdin=subprocess.DEVNULL, stdout=None, stderr=None,
                start_new_session=(os.name != "nt"),
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
            )
            self.processes[worker_id] = process
            self.stopped.discard(worker_id)
            logger.info('{"event":"worker.process_started","worker_id":"%s","pid":%s}', worker_id, process.pid)
            return process.pid

    def kill_worker(self, worker_id: str) -> int:
        with self.lock:
            process = self.processes.get(worker_id)
            if not process or process.poll() is not None:
                raise ValueError("Worker process is not running")
            self.stopped.add(worker_id)
            self._terminate_tree(process, force=True)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            return process.pid

    @staticmethod
    def _terminate_tree(process: subprocess.Popen, *, force: bool) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            command = ["taskkill", "/PID", str(process.pid), "/T"]
            if force:
                command.append("/F")
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)

    def state(self) -> dict[str, bool]:
        with self.lock:
            return {worker_id: process.poll() is None for worker_id, process in self.processes.items()}

    def _monitor(self) -> None:
        while not self.monitor_stop.wait(0.5):
            with self.lock:
                for worker_id, process in list(self.processes.items()):
                    if process.poll() is not None and worker_id not in self.stopped:
                        logger.warning('{"event":"worker.process_exited","worker_id":"%s","code":%s}',
                                       worker_id, process.returncode)
                        self.stopped.add(worker_id)

    def close(self) -> None:
        self.monitor_stop.set()
        if self.monitor:
            self.monitor.join(timeout=2)
        with self.lock:
            for process in self.processes.values():
                if process.poll() is None:
                    self._terminate_tree(process, force=True)
            deadline = time.monotonic() + 3
            for process in self.processes.values():
                if process.poll() is None:
                    try:
                        process.wait(timeout=max(0.1, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        self._terminate_tree(process, force=True)
