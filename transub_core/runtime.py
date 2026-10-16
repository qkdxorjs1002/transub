"""File-local progress and cooperative cancellation for folder workers."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

_prefix = ContextVar("transub_progress_prefix", default="")
_cancel = ContextVar("transub_cancel_event", default=None)
_print_lock = threading.Lock()


@contextmanager
def file_context(prefix: str, cancel: threading.Event):
    prefix_token = _prefix.set(prefix)
    cancel_token = _cancel.set(cancel)
    try:
        check_cancelled()
        yield
    finally:
        _cancel.reset(cancel_token)
        _prefix.reset(prefix_token)


def check_cancelled() -> None:
    event = _cancel.get()
    if event is not None and event.is_set():
        raise KeyboardInterrupt


def log(message: str, *, file=None) -> None:
    prefix = _prefix.get()
    with _print_lock:
        if prefix:
            message = "\n".join(f"{prefix} {line}" for line in message.splitlines() or [""])
        print(message, file=file, flush=True)


def iter_output_lines(stream):
    """Read packet output without blocking cancellation on an incomplete line."""
    if _cancel.get() is None:
        yield from stream
        return
    pending = b""
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while True:
            check_cancelled()
            if not selector.select(timeout=0.2):
                continue
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                if pending:
                    yield pending.decode("utf-8")
                return
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                check_cancelled()
                yield line.decode("utf-8") + "\n"


def terminate(process: subprocess.Popen) -> None:
    """Reap the child and its private process group on errors or cancellation."""
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def communicate(process: subprocess.Popen, input=None, *, timeout: float):
    if _cancel.get() is None:
        return process.communicate(input, timeout=timeout)
    deadline = time.monotonic() + timeout
    first = True
    while True:
        check_cancelled()
        remaining = deadline - time.monotonic()
        try:
            return process.communicate(input if first else None, timeout=max(0, min(0.2, remaining)))
        except subprocess.TimeoutExpired as exc:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, timeout, output=exc.output,
                                                stderr=exc.stderr) from exc
            first = False


def run_captured(command, *, timeout: float, **kwargs) -> subprocess.CompletedProcess:
    """Keep single-file behavior; poll worker children so Ctrl-C can stop them."""
    check_cancelled()
    if _cancel.get() is None:
        return subprocess.run(command, capture_output=True, timeout=timeout, **kwargs)
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=os.name != "nt", **kwargs) as process:
        try:
            stdout, stderr = communicate(process, timeout=timeout)
            check_cancelled()
        except BaseException:
            terminate(process)
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
