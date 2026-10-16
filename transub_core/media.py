"""FFmpeg operations with argument arrays, stream copy and no source overwrite."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .languages import LANGUAGES, get_language, normalize_code
from .runtime import check_cancelled, communicate, iter_output_lines, run_captured, terminate

TEXT_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text"}


class MediaError(RuntimeError):
    pass


def run(command: list[str], timeout: float = 600) -> str:
    try:
        result = run_captured(command, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"{command[0]} 실행 실패: {exc}") from exc
    if result.returncode:
        raise MediaError(f"{command[0]} 실패 ({result.returncode}): {result.stderr[-3000:]}")
    return result.stdout


def probe(path: Path, ffprobe: str = "ffprobe") -> dict:
    info = json.loads(run([ffprobe, "-v", "error", "-show_streams", "-show_data_hash", "sha256", "-show_format",
                           "-show_chapters", "-of", "json", str(path)]))
    hevc = {s["index"]: s for s in info["streams"]
            if s.get("codec_type") == "video" and s.get("codec_name") == "hevc"}
    if hevc:
        # Fetch only video codec data; attachment/font dumps can be very large.
        data = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v", "-show_entries",
                               "stream=index,codec_name,extradata", "-show_data", "-of", "json", str(path)]))
        for stream in data.get("streams", []):
            if stream.get("index") in hevc and stream.get("codec_name") == "hevc":
                hevc[stream["index"]]["extradata"] = stream.get("extradata")
    return info


def language(stream: dict) -> str:
    tags = {k.lower(): str(v) for k, v in stream.get("tags", {}).items()}
    value = normalize_code(tags.get("language", "und"))
    if value in ("und", "", "unknown"):
        title = tags.get("title", "").lower()
        for code, pattern in (("ja", r"日本語|japanese|\bjpn\b"),
                              ("en", r"english|\beng\b"), ("ko", r"한국어|korean|\bkor\b")):
            if re.search(pattern, title):
                return code
        for item in LANGUAGES.values():
            if re.search(r"\b" + re.escape(item.name.lower()) + r"\b", title):
                return item.code
    return value or "und"


def select_track(streams: list[dict], explicit: int | None = None, target_language: str = "ko") -> dict:
    target = get_language(target_language)
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    if explicit is not None:
        found = [s for s in subtitles if s["index"] == explicit]
        if not found:
            raise MediaError(f"자막 스트림 index={explicit}가 없습니다.")
        if found[0].get("codec_name") not in TEXT_CODECS:
            raise MediaError("선택한 자막은 이미지 자막입니다. OCR은 지원하지 않습니다.")
        return found[0]
    candidates = [s for s in subtitles if s.get("codec_name") in TEXT_CODECS and language(s) != target.code]
    if not candidates:
        raise MediaError("번역할 텍스트 내장 자막이 없습니다. 이미지 자막(PGS/VobSub)은 OCR이 필요합니다.")

    def rank(stream: dict) -> tuple:
        title = stream.get("tags", {}).get("title", "").lower()
        partial = bool(stream.get("disposition", {}).get("forced") or
                       re.search(r"\b(signs?|songs?|forced)\b|표지|간판", title))
        return ({"ja": 0, "en": 1}.get(language(stream), 2), partial,
                not stream.get("disposition", {}).get("default", 0), stream["index"])

    return min(candidates, key=rank)


def extract(path: Path, stream: dict, target: Path, ffmpeg: str = "ffmpeg") -> None:
    codec = "copy" if stream.get("codec_name") == "ass" else "ass"
    run([ffmpeg, "-nostdin", "-v", "error", "-n", "-copyts", "-i", str(path),
         "-map", f"0:{stream['index']}", "-c:s", codec, "-f", "ass", str(target)])


def mux(path: Path, subtitle: Path, target: Path, info: dict, ffmpeg: str = "ffmpeg",
        default_korean: bool = False, font_file: Path | None = None,
        replace_subtitle_index: int | None = None, target_language: str = "ko") -> None:
    """Attach static files explicitly; mapping them as streams can delay subtitle delivery."""
    target_lang = get_language(target_language)
    attachments = [s for s in info["streams"] if s.get("codec_type") == "attachment"]
    if replace_subtitle_index is not None and not any(
            s["index"] == replace_subtitle_index and s["codec_type"] == "subtitle" for s in info["streams"]):
        raise MediaError("교체할 자막 트랙이 없습니다.")
    count = sum(s.get("codec_type") == "subtitle" for s in info["streams"]) - int(replace_subtitle_index is not None)
    with tempfile.TemporaryDirectory(prefix=".transub-attachments-", dir=target.parent) as directory:
        files = [Path(directory) / f"attachment-{i:04d}.bin" for i in range(len(attachments))]
        if attachments:
            command = [ffmpeg, "-nostdin", "-v", "error", "-n"]
            for stream, file in zip(attachments, files):
                command += [f"-dump_attachment:{stream['index']}", str(file)]
            command += ["-i", str(path), "-map", "0:v:0?", "-t", "0", "-c", "copy", "-f", "null", "-"]
            run(command)
            for stream, file in zip(attachments, files):
                if not file.exists() or file.stat().st_size != stream.get("extradata_size"):
                    raise MediaError("기존 첨부파일 추출 크기가 일치하지 않습니다.")
        _mux_with_attachments(path, subtitle, target, info, ffmpeg, default_korean, font_file,
                              replace_subtitle_index, count, attachments, files, target_lang)


def _mux_with_attachments(path, subtitle, target, info, ffmpeg, default_korean, font_file,
                          replace_subtitle_index, count, attachments, files, target_lang):
    command = [ffmpeg, "-nostdin", "-v", "error", "-n", "-copyts", "-i", str(path),
               "-i", str(subtitle), "-map", "0"]
    if attachments:
        command += ["-map", "-0:t"]
    if replace_subtitle_index is not None:
        command += ["-map", f"-0:{replace_subtitle_index}"]
    command += ["-map", "1:0", "-map_metadata", "0",
               "-map_chapters", "0", "-c", "copy", "-copy_unknown", "-avoid_negative_ts", "disabled",
               f"-metadata:s:s:{count}", f"language={target_lang.iso639_2}", f"-metadata:s:s:{count}",
               f"title={target_lang.track_title}", f"-disposition:s:{count}", "default" if default_korean else "0"]
    if default_korean:
        for i in range(count):
            command += [f"-disposition:s:{i}", "-default"]
    for i, (stream, file) in enumerate(zip(attachments, files)):
        command += ["-attach", str(file)]
        for name, value in stream.get("tags", {}).items():
            command += [f"-metadata:s:t:{i}", f"{name}={value}"]
    if font_file:
        attached = len(attachments)
        mime = "application/vnd.ms-opentype" if font_file.suffix.lower() == ".otf" else "application/x-truetype-font"
        command += ["-attach", str(font_file), f"-metadata:s:t:{attached}", f"mimetype={mime}",
                    f"-metadata:s:t:{attached}", f"filename={font_file.name}"]
    # A time-limited interleave flush can put A/V ahead of a sparse subtitle input.
    run(command + ["-max_interleave_delta", "0", "-f", "matroska", str(target)], timeout=3600)


def verify_interleaving(path: Path, info: dict, subtitle_index: int,
                        expected_packets: int, ffprobe: str = "ffprobe") -> dict:
    """Validate delivery order, not a subtitle-only scan that hides late packets."""
    check_cancelled()
    av = {s["index"] for s in info["streams"] if s["codec_type"] in ("video", "audio")}
    command = [ffprobe, "-v", "error", "-show_packets", "-show_entries",
               "packet=stream_index,pts_time,dts_time", "-of", "compact", str(path)]
    with tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True,
                                   encoding="utf-8", stdin=subprocess.DEVNULL,
                                   start_new_session=os.name != "nt")
        try:
            report = check_packet_delivery(iter_output_lines(process.stdout), av, subtitle_index, expected_packets)
            communicate(process, timeout=30)
            check_cancelled()
            if process.returncode:
                errors.seek(0)
                raise MediaError(f"패킷 순서 검사 실패: {errors.read()[-1000:]}")
            return report
        finally:
            terminate(process)
            if process.stdout:
                process.stdout.close()


def check_packet_delivery(lines, av_indices: set[int], subtitle_index: int, expected_packets: int) -> dict:
    latest_av = None
    count, max_lag = 0, 0.0
    for line in lines:
        fields = dict(part.split("=", 1) for part in line.strip().split("|") if "=" in part)
        if "stream_index" not in fields:
            continue
        index = int(fields["stream_index"])
        timestamp = fields.get("dts_time", fields.get("pts_time", "N/A"))
        if timestamp == "N/A":
            timestamp = fields.get("pts_time", "N/A")
        if index in av_indices and timestamp != "N/A":
            value = float(timestamp)
            latest_av = value if latest_av is None else max(latest_av, value)
        if index == subtitle_index:
            count += 1
            pts = fields.get("pts_time", "N/A")
            if pts == "N/A":
                raise MediaError("자막 패킷의 표시 시간이 없습니다.")
            lag = max(0.0, latest_av - float(pts)) if latest_av is not None else 0.0
            max_lag = max(max_lag, lag)
            # Allow ordinary decoder reordering/buffering, reject packets delivered after their scene.
            if lag > 1.0:
                raise MediaError(f"자막 패킷 배치 오류: {count}번째 자막({float(pts):.3f}초)이 "
                                 f"영상·음성보다 {lag:.3f}초 늦게 전달됩니다.")
    if count != expected_packets:
        raise MediaError(f"자막 패킷 수 불일치: {count} != {expected_packets}")
    return {"subtitle_packets": count, "subtitle_delivery_verified": True,
            "max_subtitle_delivery_lag_seconds": round(max_lag, 3)}


def _hevc_configuration(stream: dict, clear_completeness: bool = False) -> bytes | None:
    """Validate ffprobe's hvcC dump; optionally clear only array completeness bits."""
    value = stream.get("extradata")
    if not isinstance(value, str):
        return None
    data = bytearray()
    try:
        for line in value.splitlines():
            if not line.strip():
                continue
            address, contents = line.split(": ", 1)
            chunk = bytes.fromhex(contents.split("  ", 1)[0])
            if int(address, 16) != len(data) or not 0 < len(chunk) <= 16:
                return None
            data.extend(chunk)
    except ValueError:
        return None
    if (len(data) != stream.get("extradata_size") or len(data) < 23 or data[0] != 1 or
            stream.get("extradata_hash") != "SHA256:" + hashlib.sha256(data).hexdigest()):
        return None
    result = bytearray(data)
    position = 23
    for _ in range(data[22]):
        if position + 3 > len(data) or data[position] & 0x40:
            return None
        nal_type = data[position] & 0x3f
        if clear_completeness:
            result[position] &= 0x7f
        count = int.from_bytes(data[position + 1:position + 3], "big")
        position += 3
        for _ in range(count):
            if position + 2 > len(data):
                return None
            size = int.from_bytes(data[position:position + 2], "big")
            position += 2
            if (size < 2 or position + size > len(data) or
                    (data[position] >> 1) & 0x3f != nal_type):
                return None
            position += size
    return bytes(result) if position == len(data) else None


def verify_streams(before: dict, after: dict, extra_font: bool = False, target_language: str = "ko") -> dict:
    target_lang = get_language(target_language)
    old = before["streams"]
    new = after["streams"]
    if len(new) != len(old) + 1 + int(extra_font):
        raise MediaError("리먹스 후 스트림 수가 일치하지 않습니다.")
    # Containers may regroup attachments, so compare each stream type in its own order.
    for kind in ("video", "audio", "subtitle", "attachment", "data"):
        a = [s for s in old if s["codec_type"] == kind]
        b = [s for s in new if s["codec_type"] == kind]
        for left, right in zip(a, b):
            keys = ["codec_name", "width", "height", "sample_rate", "channels", "extradata_size", "extradata_hash"]
            changed = [key for key in keys if left.get(key) != right.get(key)]
            if changed:
                # Matroska's HEVC writer clears hvcC array_completeness (1 -> 0).
                # Every other byte must match, and the pipeline still requires A/V hashes.
                expected = None
                if (kind == "video" and left.get("codec_name") == right.get("codec_name") == "hevc" and
                        changed == ["extradata_hash"] and
                        "matroska" in after.get("format", {}).get("format_name", "").split(",")):
                    expected = _hevc_configuration(left, clear_completeness=True)
                if expected is None or expected != _hevc_configuration(right):
                    raise MediaError(f"원본 {kind} 스트림 특성이 변경되었습니다. ({', '.join(changed)})")
            if kind == "attachment" and left.get("tags", {}) != right.get("tags", {}):
                raise MediaError("원본 첨부파일 메타데이터가 변경되었습니다.")
    subs = [s for s in new if s["codec_type"] == "subtitle"]
    if not subs or language(subs[-1]) != target_lang.code or subs[-1].get("codec_name") != "ass":
        raise MediaError(f"추가된 {target_lang.label} ASS 트랙을 찾지 못했습니다.")
    if len(before.get("chapters", [])) != len(after.get("chapters", [])):
        raise MediaError("챕터 수가 변경되었습니다.")
    return {"original_streams": len(old), "output_streams": len(new),
            "korean_stream_index" if target_lang.code == "ko" else "translated_stream_index": subs[-1]["index"],
            "stream_copy": True}


def av_hash(path: Path, ffmpeg: str = "ffmpeg") -> str:
    return run([ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v?", "-map", "0:a?",
                "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-"], timeout=3600)
