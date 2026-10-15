"""Lossless ASS dialogue editing: never merge, retime, or flatten events."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


class SubtitleError(ValueError):
    pass


def milliseconds(value: str) -> int:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})", value.strip())
    if not match:
        raise SubtitleError(f"잘못된 자막 시간: {value!r}")
    h, m, s, frac = match.groups()
    if int(m) >= 60 or int(s) >= 60:
        raise SubtitleError(f"잘못된 자막 시간: {value!r}")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0"))


TOKEN_RE = re.compile(r"⟦P\d+⟧")
CONTROL_RE = re.compile(r"\{[^{}]*\}|\\[Nnh]")


def protect(text: str) -> tuple[str, list[str]]:
    """Keep ASS overrides, line breaks and vector drawings outside model control."""
    if TOKEN_RE.search(text):
        raise SubtitleError("원문이 내부 보호 토큰과 충돌합니다.")
    tokens: list[str] = []
    result: list[str] = []
    drawing = False

    def add(value: str) -> None:
        result.append(f"⟦P{len(tokens)}⟧")
        tokens.append(value)

    position = 0
    for match in CONTROL_RE.finditer(text):
        before = text[position:match.start()]
        if before:
            add(before) if drawing else result.append(before)
        control = match.group()
        add(control)
        if control.startswith("{"):
            modes = re.findall(r"\\p(\d+)(?!\d)", control)
            if modes:
                drawing = int(modes[-1]) != 0
        position = match.end()
    rest = text[position:]
    if rest:
        add(rest) if drawing else result.append(rest)
    return "".join(result), tokens


def restore(source: str, translated: str, tokens: list[str]) -> str:
    if not isinstance(translated, str) or not translated.strip():
        raise SubtitleError("빈 번역 결과")
    expected = [f"⟦P{i}⟧" for i in range(len(tokens))]
    if TOKEN_RE.findall(translated) != expected:
        raise SubtitleError("ASS 보호 토큰이 누락·중복·재배열되었습니다.")
    plain = TOKEN_RE.sub("", translated)
    if any(c in plain for c in ("{", "}", "\\", "⟦", "⟧")) or any(
            ord(c) < 32 or c in "\x7f\x85\u2028\u2029" for c in plain):
        raise SubtitleError("번역에 새 ASS 제어문자 또는 개행이 들어 있습니다.")
    if TOKEN_RE.sub("", source).strip() and not plain.strip():
        raise SubtitleError("대사가 제거되었습니다.")
    return TOKEN_RE.sub(lambda m: tokens[int(m.group()[2:-1])], translated)


@dataclass
class Cue:
    id: str
    line_index: int
    fields: list[str]
    format: list[str]
    prefix: str
    ending: str
    start: int
    end: int
    text: str
    protected: str
    tokens: list[str]
    overlaps: list[str] = field(default_factory=list)

    def get(self, key: str) -> str:
        return self.fields[self.format.index(key)] if key in self.format else ""

    @property
    def translatable(self) -> bool:
        return bool(TOKEN_RE.sub("", self.protected).strip())

    def payload(self) -> dict:
        return {"id": self.id, "start_ms": self.start, "end_ms": self.end,
                "speaker": self.get("name") or self.get("actor"),
                "style": self.get("style"), "layer": self.get("layer"),
                "overlaps": self.overlaps, "text": self.protected}

    def text_spans(self) -> dict[str, str]:
        """Only ordinary, nonblank text is writable; all boundaries stay local."""
        return {f"s{i}": part for i, part in enumerate(TOKEN_RE.split(self.protected)) if part.strip()}

    def translation_payload(self) -> dict:
        item = self.payload()
        del item["text"]
        layout = []
        position = 0
        for i, match in enumerate(TOKEN_RE.finditer(self.protected)):
            part = self.protected[position:match.start()]
            if part.strip():
                layout.append({"id": f"s{i}", "text": part})
            elif part:
                layout.append({"fixed": "whitespace"})
            control = self.tokens[int(match.group()[2:-1])]
            kind = {r"\N": "line_break", r"\n": "soft_line_break", r"\h": "nonbreaking_space"}.get(control)
            layout.append({"fixed": kind or ("format_change" if control.startswith("{") else "drawing")})
            position = match.end()
        last = self.protected[position:]
        if last.strip():
            layout.append({"id": f"s{len(self.tokens)}", "text": last})
        item["layout"] = layout
        return item

    def assemble_spans(self, values: dict[str, str]) -> str:
        expected = self.text_spans()
        if not isinstance(values, dict) or set(values) != set(expected):
            raise SubtitleError(f"{self.id}: 번역 텍스트 구간 ID가 일치하지 않습니다.")
        for identifier, value in values.items():
            try:
                restore(expected[identifier], value, [])
            except SubtitleError as exc:
                raise SubtitleError(f"{self.id}/{identifier}: {exc}") from exc
        # Insert text only into its original gap, never inside a tag/drawing.
        pieces = []
        for i, part in enumerate(TOKEN_RE.split(self.protected)):
            pieces.append(values.get(f"s{i}", part))
            if i < len(self.tokens):
                pieces.append(f"⟦P{i}⟧")
        result = "".join(pieces)
        restore(self.protected, result, self.tokens)
        return result


class AssDocument:
    def __init__(self, text: str):
        self.lines = text.splitlines(keepends=True)
        self.cues: list[Cue] = []
        section, event_format = "", []
        for index, line in enumerate(self.lines):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped.lower()
            if section != "[events]":
                continue
            if stripped.lower().startswith("format:"):
                event_format = [s.strip().lower() for s in stripped.split(":", 1)[1].split(",")]
                if not {"start", "end", "text"}.issubset(event_format) or event_format[-1] != "text":
                    raise SubtitleError("지원하지 않는 ASS Events Format (Text는 마지막이어야 함)")
            match = re.match(r"(\s*Dialogue:\s*)(.*?)(\r?\n)?$", line, re.IGNORECASE)
            if not match:
                continue
            if not event_format:
                raise SubtitleError("ASS Events Format이 없습니다.")
            fields = match[2].split(",", len(event_format) - 1)
            if len(fields) != len(event_format):
                raise SubtitleError(f"잘못된 ASS Dialogue: {index + 1}행")
            start, end = (milliseconds(fields[event_format.index(k)]) for k in ("start", "end"))
            if end < start:
                raise SubtitleError("자막 종료가 시작보다 빠릅니다.")
            raw = fields[-1]
            protected, tokens = protect(raw)
            self.cues.append(Cue(f"c{len(self.cues):06d}", index, fields, event_format[:],
                                 match[1], match[3] or "", start, end, raw, protected, tokens))
        if not self.cues:
            raise SubtitleError("번역 가능한 ASS Dialogue가 없습니다.")
        active: list[Cue] = []
        for cue in sorted(self.cues, key=lambda c: (c.start, c.end, c.id)):
            active = [other for other in active if other.end > cue.start]
            for other in active:
                if cue.end > other.start and cue.end > cue.start:
                    cue.overlaps.append(other.id)
                    other.overlaps.append(cue.id)
            if cue.end > cue.start:
                active.append(cue)

    def render(self, translations: dict[str, str], font: str | None = None) -> str:
        expected = {cue.id for cue in self.cues if cue.translatable}
        if set(translations) != expected:
            raise SubtitleError("전체 자막 ID와 번역 ID가 일치하지 않습니다.")
        lines = self.lines[:]
        for cue in self.cues:
            if cue.translatable:
                value = restore(cue.protected, translations[cue.id], cue.tokens)
                lines[cue.line_index] = cue.prefix + ",".join(cue.fields[:-1] + [value]) + cue.ending
        if font:
            if any(c in font for c in ",\n\r{}\\"):
                raise SubtitleError("잘못된 폰트 이름")
            section, style_format = "", []
            for i, line in enumerate(lines):
                if line.strip().startswith("["):
                    section = line.strip().lower()
                if section not in ("[v4+ styles]", "[v4 styles]"):
                    continue
                if line.lower().startswith("format:"):
                    style_format = [s.strip().lower() for s in line.split(":", 1)[1].split(",")]
                if line.lower().startswith("style:") and "fontname" in style_format:
                    prefix, values = line.split(":", 1)
                    ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                    fields = values.rstrip("\r\n").split(",")
                    fields[style_format.index("fontname")] = font
                    lines[i] = prefix + ":" + ",".join(fields) + ending
        return "".join(lines)

    def verify_structure(self, translated: "AssDocument") -> dict:
        if len(self.cues) != len(translated.cues):
            raise SubtitleError("번역 전후 이벤트 수가 달라졌습니다.")
        for a, b in zip(self.cues, translated.cues):
            if a.fields[:-1] != b.fields[:-1] or a.tokens != b.tokens:
                raise SubtitleError(f"{a.id}: 시간/화자/레이어/태그/드로잉 변경")
        return {"events": len(self.cues), "translated_events": sum(c.translatable for c in self.cues),
                "overlap_pairs": sum(len(c.overlaps) for c in self.cues) // 2,
                "timing_style_layer_tags_preserved": True}


def batches(cues: list[Cue], size: int) -> list[list[Cue]]:
    """Chronological chunks; overlapping neighbours are also supplied as context."""
    if size < 1:
        raise ValueError("batch size must be positive")
    ordered = sorted((c for c in cues if c.translatable), key=lambda c: (c.start, c.end, c.id))
    return [ordered[i:i + size] for i in range(0, len(ordered), size)]


def context_for(all_cues: list[Cue], batch: list[Cue], radius: int = 6) -> list[Cue]:
    ordered = sorted(all_cues, key=lambda c: (c.start, c.end, c.id))
    targets = {c.id for c in batch}
    positions = [i for i, c in enumerate(ordered) if c.id in targets]
    neighbours = {c.id for c in ordered[max(0, min(positions) - radius):max(positions) + radius + 1]}
    neighbours.update(other for c in batch for other in c.overlaps)
    return [c for c in ordered if c.id in neighbours and c.id not in targets]
