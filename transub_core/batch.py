"""Sequential folder processing through the existing single-video pipeline."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .codex import CodexError
from .media import MediaError
from .pipeline import ExistingKoreanError, digest, process
from .subtitles import SubtitleError

PROCESS_ERRORS = (CodexError, MediaError, SubtitleError, OSError, ValueError,
                  subprocess.TimeoutExpired)


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
    results = []
    counts = {"completed": 0, "skipped": 0, "failed": 0}
    for index, video in enumerate(videos, 1):
        print(f"[{index}/{len(videos)}] {video.relative_to(folder)}", file=sys.stderr, flush=True)
        item_args = argparse.Namespace(**vars(args))
        item_args.video = str(video)
        if args.work_dir:
            item_args.work_dir = str(Path(args.work_dir).expanduser() /
                                     (video.stem[:80] + "-" + digest(str(video))[:10]))
        item = {"video": str(video)}
        try:
            item.update(status="completed", result=process(item_args))
        except ExistingKoreanError as exc:
            item.update(status="skipped", reason=str(exc))
            print(f"건너뜀: {video.name}: {exc}", file=sys.stderr, flush=True)
        except PROCESS_ERRORS as exc:
            item.update(status="failed", error=str(exc))
            print(f"실패: {video.name}: {exc}", file=sys.stderr, flush=True)
        counts[item["status"]] += 1
        results.append(item)
    return {"status": "failed" if counts["failed"] else "completed",
            "folder": str(folder), "total": len(videos), **counts, "results": results}


def process_input(args) -> dict:
    input_path = Path(args.video).expanduser().absolute()
    if input_path.is_symlink():
        raise MediaError("심볼릭 링크 대신 실제 영상/폴더 경로를 지정하세요.")
    if input_path.is_dir():
        return process_folder(args, input_path.resolve(strict=True))
    return process(args)
