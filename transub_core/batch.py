"""Bounded parallel folder processing through the single-video pipeline."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .codex import CodexError
from .media import MediaError
from .pipeline import ExistingTargetLanguageError, job_directory_name, process
from .runtime import file_context, log
from .subtitles import SubtitleError

PROCESS_ERRORS = (CodexError, MediaError, SubtitleError, OSError, ValueError,
                  subprocess.TimeoutExpired)
DEFAULT_JOBS = 2


def discover_videos(folder: Path) -> list[Path]:
    """Snapshot the tree before processing; never follow links or remux workdirs."""
    def raise_walk_error(error):
        raise error

    videos = []
    for directory, names, files in os.walk(folder, followlinks=False, onerror=raise_walk_error):
        current = Path(directory)
        names[:] = [name for name in names
                    if not name.startswith(".transub-mux-") and not (current / name).is_symlink()]
        for name in files:
            path = current / name
            if path.suffix.lower() == ".mkv" and not path.is_symlink() and path.is_file():
                videos.append(path)
    return sorted(videos, key=lambda path: (path.relative_to(folder).as_posix().casefold(),
                                            path.relative_to(folder).as_posix()))


def process_folder(args, folder: Path) -> dict:
    if args.output:
        raise MediaError("폴더 입력에는 --output을 사용할 수 없습니다. 파일별 원본 MKV를 교체합니다.")
    videos = discover_videos(folder)
    if not videos:
        raise MediaError("폴더와 하위 폴더에 처리할 MKV 파일이 없습니다.")
    jobs = getattr(args, "jobs", DEFAULT_JOBS)
    if not isinstance(jobs, int) or jobs < 1:
        raise MediaError("--jobs는 1 이상이어야 합니다.")
    cancel = threading.Event()

    def process_video(index, video):
        prefix = f"[{index + 1}/{len(videos)}] {video.relative_to(folder)}"
        item_args = argparse.Namespace(**vars(args))
        item_args.video = str(video)
        if args.work_dir:
            item_args.work_dir = str(Path(args.work_dir).expanduser() /
                                     job_directory_name(video, getattr(args, "target_language", "ko")))
        with file_context(prefix, cancel):
            log("처리 시작", file=sys.stderr)
            item = {"video": str(video)}
            try:
                item.update(status="completed", result=process(item_args))
            except ExistingTargetLanguageError as exc:
                item.update(status="skipped", reason=str(exc))
                log(f"건너뜀: {exc}", file=sys.stderr)
            except PROCESS_ERRORS as exc:
                item.update(status="failed", error=str(exc))
                log(f"실패: {exc}", file=sys.stderr)
            except BaseException:
                cancel.set()
                raise
            return item

    if jobs == 1 or len(videos) == 1:
        results = [process_video(index, video) for index, video in enumerate(videos)]
    else:
        results = [None] * len(videos)
        pool = ThreadPoolExecutor(max_workers=min(jobs, len(videos)), thread_name_prefix="transub")
        pending = {}
        next_index = 0
        try:
            while next_index < len(videos) or pending:
                while next_index < len(videos) and len(pending) < jobs and not cancel.is_set():
                    future = pool.submit(process_video, next_index, videos[next_index])
                    pending[future] = next_index
                    next_index += 1
                if not pending:
                    raise KeyboardInterrupt
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    results[pending.pop(future)] = future.result()
        except BaseException:
            cancel.set()
            for future in pending:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    counts = {"completed": 0, "skipped": 0, "failed": 0}
    for item in results:
        counts[item["status"]] += 1
    return {"status": "failed" if counts["failed"] else "completed",
            "folder": str(folder), "total": len(videos), **counts, "results": results}


def process_input(args) -> dict:
    input_path = Path(args.video).expanduser().absolute()
    if input_path.is_symlink():
        raise MediaError("심볼릭 링크 대신 실제 영상/폴더 경로를 지정하세요.")
    if input_path.is_dir():
        return process_folder(args, input_path.resolve(strict=True))
    return process(args)
