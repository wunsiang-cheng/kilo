"""TaskManager: runs shell commands and keeps their records and logs."""

import asyncio
import logging
import os
import signal
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kilo.config import settings
from kilo.models.task import ShellResult, TaskInfo, TaskStatus

logger = logging.getLogger(__name__)


async def terminate(proc: asyncio.subprocess.Process) -> None:
    """
    End a command and every process it started, and wait (up to 10 seconds) until it has exited.
    On POSIX the command leads its own process group (start_new_session): SIGTERM, then SIGKILL after
    a second. Windows has no process groups to signal, so `taskkill /T` ends the process tree.
    """
    if proc.returncode is None:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), 1)
                except asyncio.TimeoutError:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass  # already gone
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        pass


async def _terminate_once_started(spawn: "asyncio.Future[asyncio.subprocess.Process]") -> None:
    """End the command that `spawn` is starting, as soon as it has started (if it does)."""
    try:
        proc = await spawn
    except Exception:
        return  # it never started
    await terminate(proc)


MAX_FINISHED_TASKS = 200  # finished tasks kept in memory (newest); older ones are forgotten
LOG_DAYS = 7  # task logs older than this are deleted
PRUNE_EVERY = 3600.0  # seconds between sweeps for old logs


class _Capped:
    """A command's output kept within `limit` bytes: its start and its end, with what was left out counted."""

    def __init__(self, limit: int):
        self.limit, self.head, self.tail, self.dropped = limit, bytearray(), bytearray(), 0

    def add(self, chunk: bytes) -> None:
        room = self.limit - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        self.tail += chunk
        if len(self.tail) > self.limit:
            cut = len(self.tail) - self.limit
            self.dropped += cut
            del self.tail[:cut]

    def text(self) -> str:
        gap = f"\n[… {self.dropped:,} bytes of output omitted …]\n" if self.dropped else ""
        return self.head.decode("utf-8", errors="replace") + gap + self.tail.decode("utf-8", errors="replace")


async def _read_into(stream: Optional[asyncio.StreamReader], sink: _Capped) -> None:
    while stream is not None and (chunk := await stream.read(65536)):
        sink.add(chunk)


class TaskManager:
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or settings.kilo_data_dir
        self.tasks_dir = self.data_dir / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: Dict[str, TaskInfo] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._terminations: Set[asyncio.Task] = set()  # kills started for cancelled commands, kept until done
        self._pruned_at = float("-inf")

    def _generate_task_id(self) -> str:
        return f"task_{uuid.uuid4().hex[:8]}"

    def _get_log_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.log"

    def _track(self, info: TaskInfo) -> None:
        """Keep a new task, forgetting the oldest finished ones and old log files, so neither grows for ever."""
        self._tasks[info.task_id] = info
        finished = [t for t in self._tasks.values() if t.status != TaskStatus.RUNNING]
        if len(finished) > MAX_FINISHED_TASKS:
            finished.sort(key=lambda t: t.start_time)
            for old in finished[: len(finished) - MAX_FINISHED_TASKS]:
                del self._tasks[old.task_id]
        now = time.monotonic()
        if now - self._pruned_at >= PRUNE_EVERY:
            self._pruned_at = now
            cutoff = time.time() - LOG_DAYS * 86400
            for log in self.tasks_dir.glob("*.log"):
                try:
                    if log.stat().st_mtime < cutoff and log.stem not in self._tasks:
                        log.unlink()
                except OSError:
                    pass

    async def run(
        self,
        command: str,
        timeout: Optional[int] = None,
        cwd: Optional[Path] = None,
    ) -> ShellResult:
        """Run a shell command with a timeout."""
        timeout = timeout or settings.kilo_default_timeout
        work_dir = str(cwd or settings.kilo_workspace)
        task_id = self._generate_task_id()
        log_path = self._get_log_path(task_id)

        start_time = time.monotonic()
        start_dt = datetime.now(timezone.utc)

        info = TaskInfo(
            task_id=task_id,
            command=command,
            status=TaskStatus.RUNNING,
            start_time=start_dt,
            log_file=str(log_path),
        )
        self._track(info)

        # Run in a new process group so we can terminate all child processes on timeout. Started as a
        # task of its own: cancelled while it starts, the process still starts, and can then be killed.
        spawn = asyncio.ensure_future(asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            start_new_session=True,
        ))
        try:
            proc = await asyncio.shield(spawn)
            info.pid = proc.pid
            self._processes[task_id] = proc

            # Output is kept within the tool output limit as it arrives, so a flood of it cannot fill memory.
            stdout_sink = _Capped(settings.kilo_tool_output_max_chars)
            stderr_sink = _Capped(settings.kilo_tool_output_max_chars)
            try:
                await asyncio.wait_for(
                    asyncio.gather(_read_into(proc.stdout, stdout_sink), _read_into(proc.stderr, stderr_sink), proc.wait()),
                    timeout=float(timeout),
                )
                duration = time.monotonic() - start_time
                exit_code = proc.returncode

                stdout_str = stdout_sink.text()
                stderr_str = stderr_sink.text()

                status = TaskStatus.COMPLETED if exit_code == 0 else TaskStatus.FAILED
                info.status = status
                info.exit_code = exit_code
                info.end_time = datetime.now(timezone.utc)

                # Write log file
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(f"=== Command: {command} ===\n")
                    f.write(f"=== Directory: {work_dir} ===\n")
                    f.write(f"=== Exit Code: {exit_code} ===\n\n")
                    f.write("--- STDOUT ---\n")
                    f.write(stdout_str)
                    f.write("\n--- STDERR ---\n")
                    f.write(stderr_str)

                return ShellResult(
                    action="run",
                    task_id=task_id,
                    status=status.value,
                    command=command,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    exit_code=exit_code,
                    duration_seconds=round(duration, 3),
                            message=f"Command finished with exit code {exit_code} in {round(duration, 2)}s",
                )

            except asyncio.TimeoutError:
                duration = time.monotonic() - start_time
                info.status = TaskStatus.TIMED_OUT
                info.end_time = datetime.now(timezone.utc)

                try:
                    await terminate(proc)
                except Exception:
                    pass

                timeout_msg = f"Command timed out after {timeout} seconds."
                stdout_str, stderr_str = stdout_sink.text(), stderr_sink.text()
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(f"=== Command: {command} ===\n")
                    f.write(f"=== TIMED OUT after {timeout}s ===\n\n")
                    f.write("--- STDOUT ---\n")
                    f.write(stdout_str)
                    f.write("\n--- STDERR ---\n")
                    f.write(stderr_str)

                return ShellResult(
                    action="run",
                    task_id=task_id,
                    status=TaskStatus.TIMED_OUT.value,
                    command=command,
                    stdout=stdout_str,  # what it printed before it was stopped
                    stderr=(stderr_str + "\n" if stderr_str else "") + timeout_msg,
                    exit_code=-1,
                    duration_seconds=round(duration, 3),
                            message=timeout_msg,
                )

        except asyncio.CancelledError:
            # The caller stopped waiting (e.g. the user stopped the reply): the command must not live on.
            # The kill runs as a task of its own, as the cancelled caller may not await anything more.
            info.status = TaskStatus.KILLED
            info.end_time = datetime.now(timezone.utc)
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"=== Command: {command} ===\n")
                f.write("=== KILLED: stopped before it finished ===\n")
            termination = asyncio.ensure_future(_terminate_once_started(spawn))
            self._terminations.add(termination)
            termination.add_done_callback(self._terminations.discard)
            raise

        except Exception as e:
            duration = time.monotonic() - start_time
            info.status = TaskStatus.FAILED
            info.end_time = datetime.now(timezone.utc)
            err_msg = f"Failed to execute command: {str(e)}"
            return ShellResult(
                action="run",
                task_id=task_id,
                status=TaskStatus.FAILED.value,
                command=command,
                stdout="",
                stderr=err_msg,
                exit_code=1,
                duration_seconds=round(duration, 3),
                    message=err_msg,
            )
        finally:
            self._processes.pop(task_id, None)

    def list_tasks(self, status: Optional[str] = None) -> List[TaskInfo]:
        """List tracked tasks, optionally filtered by status."""
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status.value.lower() == status.lower()]
        # Sort newest first
        tasks.sort(key=lambda t: t.start_time, reverse=True)
        return tasks

    def get_task(self, task_id: str) -> Optional[TaskInfo]:
        """Get task information by ID."""
        return self._tasks.get(task_id)

    def get_task_logs(self, task_id: str, tail: int = 50) -> Tuple[bool, str]:
        """Read logs for a given task ID."""
        info = self._tasks.get(task_id)
        log_path = self._get_log_path(task_id)
        if not log_path.exists():
            if info and Path(info.log_file).exists():
                log_path = Path(info.log_file)
            else:
                return False, f"Log file for task {task_id} does not exist."

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                # Streamed: only the last `tail` lines are held, however long the log.
                content = "".join(deque(f, maxlen=tail) if tail > 0 else f)
            return True, content
        except Exception as e:
            return False, f"Failed to read logs for task {task_id}: {str(e)}"
