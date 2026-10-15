#!/usr/bin/env python3
"""Add Korean embedded subtitles using an isolated, logged-in Codex CLI."""
import argparse
import sys
from pathlib import Path

from transub_core.batch import PROCESS_ERRORS, process_input
from transub_core.codex import DEFAULT_MODEL, DEFAULT_REASONING


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("1 이상이어야 합니다.")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="내장 자막을 순정 Codex로 한국어 번역하여 원본 MKV에 추가합니다.")
    p.add_argument("video", help="내장 텍스트 자막이 있는 영상 또는 폴더 (하위 폴더의 MKV도 순차 처리)")
    p.add_argument("-o", "--output", help="단일 영상의 별도 MKV 출력 경로 (폴더 입력에는 사용 불가)")
    p.add_argument("--work-dir", help="체크포인트/보고서 디렉터리 (폴더 입력은 파일별 하위 디렉터리 생성)")
    p.add_argument("--title", help="작품/시즌/회차 힌트. 생략하면 Codex가 파일명과 자막으로 조사")
    p.add_argument("--source-track", type=int, help="ffprobe의 자막 stream index (기본 일본어>영어>기타)")
    p.add_argument("--list-tracks", action="store_true", help="내장 자막 목록만 출력")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"조사·번역 Codex 모델 (기본: {DEFAULT_MODEL})")
    # Omission lets resumed jobs retain their recorded effort, including legacy None.
    p.add_argument("--reasoning", choices=["low", "medium", "high", "xhigh"], default=None,
                   help=f"추론 강도 (신규 작업 기본: {DEFAULT_REASONING}; 재개 시 저장된 설정)")
    p.add_argument("--batch-size", type=positive, default=60, help="번역 배치 이벤트 수 (기본 60)")
    p.add_argument("--timeout", type=positive, default=900, help="Codex 요청별 제한 초 (기본 900)")
    p.add_argument("--retries", type=int, choices=range(0, 6), default=2, help="번역 실패 재시도 횟수 (기본 2)")
    p.add_argument("--auth-home", help="auth.json이 있는 기존 Codex 홈 (설정은 읽지 않음)")
    p.add_argument("--font", help="한국어 ASS 기본 스타일의 Fontname 교체 (생략 시 원본 유지)")
    p.add_argument("--font-file", help="결과 MKV에 추가 첨부할 한국어 TTF/OTF/TTC 폰트")
    p.add_argument("--default-korean", action="store_true", help="한국어 트랙을 기본 자막으로 선택")
    p.add_argument("--allow-existing-korean", action="store_true", help="한국어 트랙이 있어도 새 트랙 추가")
    p.add_argument("--codex", default="codex", help="Codex CLI 실행 파일")
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--ffprobe", default="ffprobe")
    return p


def print_tracks(video, tracks: list[dict]) -> None:
    print(f"자막 목록: {video}")
    if not tracks:
        print("  내장 자막이 없습니다.")
    languages = {"ja": "일본어", "en": "영어", "ko": "한국어", "und": "언어 미상"}
    for track in tracks:
        language = track.get("language") or "und"
        details = [languages.get(language, language), track.get("codec") or "코덱 미상"]
        if track.get("title"):
            details.append(track["title"])
        if track.get("forced"):
            details.append("강제 자막")
        print(f"  트랙 {track['index']}: {' / '.join(details)}")


def print_result(args, result: dict) -> None:
    folder = Path(result["folder"]) if "folder" in result else None
    if args.list_tracks:
        if folder is None:
            print_tracks(args.video, result["subtitle_tracks"])
        else:
            for item in result["results"]:
                if item["status"] == "completed":
                    print_tracks(Path(item["video"]).relative_to(folder), item["result"]["subtitle_tracks"])
    if folder is None:
        return  # The single-video pipeline already prints its completion message.
    if args.list_tracks:
        print(f"조회 완료: 총 {result['total']}개 · 조회 {result['completed']}개 · 실패 {result['failed']}개")
    else:
        print(f"처리 완료: 총 {result['total']}개 · 완료 {result['completed']}개 · "
              f"건너뜀 {result['skipped']}개 · 실패 {result['failed']}개")
    if result["failed"]:
        print("실패한 파일:")
        for item in result["results"]:
            if item["status"] == "failed":
                print(f"  - {Path(item['video']).relative_to(folder)}: {item['error']}")
        if not args.list_tracks:
            print("같은 명령으로 다시 실행하면 저장된 번역을 재사용합니다.")


def main() -> int:
    try:
        args = parser().parse_args()
        result = process_input(args)
        print_result(args, result)
        return 1 if result.get("failed", 0) else 0
    except KeyboardInterrupt:
        print("중단됨. 검증 전 원본은 변경하지 않으며 저장된 번역은 같은 명령으로 재개합니다.", file=sys.stderr)
        return 130
    except PROCESS_ERRORS as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
