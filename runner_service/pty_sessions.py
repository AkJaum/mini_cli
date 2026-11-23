import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread, Timer


@dataclass
class PtySession:
    master_fd: int
    process: subprocess.Popen
    timer: Timer | None = None
    stopped: Event = field(default_factory=Event)
    expired: bool = False
    buffer: bytearray = field(default_factory=bytearray)
    dropped_output: bool = False
    exit_code: int | None = None
    running: bool = True


class PtySessionManager:
    def __init__(
        self,
        max_buffer_bytes: int,
        max_lifetime_seconds: int,
    ) -> None:
        self._max_buffer_bytes = max_buffer_bytes
        self._max_lifetime_seconds = max_lifetime_seconds
        self._lock = Lock()
        self._lifecycle_lock = Lock()
        self._sessions: dict[str, PtySession] = {}

    @staticmethod
    def _set_window_size(file_descriptor: int, rows: int, cols: int) -> None:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(file_descriptor, termios.TIOCSWINSZ, size)

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def start(
        self,
        key: str,
        command: list[str],
        cwd: Path,
        rows: int,
        cols: int,
        environment: dict[str, str],
    ) -> None:
        with self._lifecycle_lock:
            self._stop(key)
            self._spawn(key, command, cwd, rows, cols, environment)

    def _spawn(self, key, command, cwd, rows, cols, environment):
        master_fd, slave_fd = pty.openpty()

        try:
            self._set_window_size(slave_fd, rows, cols)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("pty_child.py")),
                    *command,
                ],
                cwd=str(cwd),
                env=environment,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)

        session = PtySession(master_fd=master_fd, process=process)
        timer = Timer(self._max_lifetime_seconds, self._expire, args=(key, session))
        timer.daemon = True
        session.timer = timer
        with self._lock:
            self._sessions[key] = session
        timer.start()

        Thread(
            target=self._read_output,
            args=(key, session),
            daemon=True,
            name=f"pty-reader-{process.pid}",
        ).start()

    def _read_output(self, key: str, session: PtySession) -> None:
        try:
            while not session.stopped.is_set():
                ready, _, _ = select.select([session.master_fd], [], [], 0.2)
                if not ready:
                    continue
                try:
                    chunk = os.read(session.master_fd, 8192)
                except OSError as exc:
                    if exc.errno in {errno.EBADF, errno.EIO}:
                        break
                    raise
                if not chunk:
                    break

                with self._lock:
                    if self._sessions.get(key) is not session:
                        break
                    session.buffer.extend(chunk)
                    overflow = len(session.buffer) - self._max_buffer_bytes
                    if overflow > 0:
                        del session.buffer[:overflow]
                        session.dropped_output = True
        finally:
            with self._lock:
                os.close(session.master_fd)
                session.master_fd = -1
            if session.stopped.is_set():
                self._terminate_process(session.process)
            exit_code = session.process.wait()
            session.timer.cancel()
            with self._lock:
                if self._sessions.get(key) is session:
                    session.exit_code = exit_code
                    session.running = False

    def _expire(self, key: str, session: PtySession) -> None:
        with self._lock:
            if self._sessions.get(key) is not session or not session.running:
                return
            session.expired = True
            session.stopped.set()
        self._terminate_process(session.process)

    def poll(self, key: str) -> dict[str, object] | None:
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            output = bytes(session.buffer)
            session.buffer.clear()
            result = {
                "output": output,
                "running": session.running,
                "exitCode": session.exit_code,
                "droppedOutput": session.dropped_output,
                "expired": session.expired,
            }
            session.dropped_output = False
            if not session.running:
                self._sessions.pop(key, None)
            return result

    def write(self, key: str, data: bytes) -> bool:
        with self._lock:
            session = self._sessions.get(key)
            if session is None or session.master_fd == -1 or session.stopped.is_set():
                return False
            master_fd = os.dup(session.master_fd)

        try:
            remaining = memoryview(data)
            while remaining:
                remaining = remaining[os.write(master_fd, remaining) :]
            return True
        except OSError as exc:
            if exc.errno in {errno.EBADF, errno.EIO}:
                return False
            raise
        finally:
            os.close(master_fd)

    def resize(self, key: str, rows: int, cols: int) -> bool:
        with self._lock:
            session = self._sessions.get(key)
            if session is None or session.master_fd == -1 or session.stopped.is_set():
                return False
            try:
                self._set_window_size(session.master_fd, rows, cols)
            except OSError as exc:
                if exc.errno in {errno.EBADF, errno.EIO}:
                    return False
                raise
            return True

    def stop(self, key: str) -> None:
        with self._lifecycle_lock:
            self._stop(key)

    def _stop(self, key: str) -> None:
        with self._lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            session.timer.cancel()
            session.stopped.set()
            self._terminate_process(session.process)
