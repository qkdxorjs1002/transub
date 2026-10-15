"""Optional headless libass visual QA. No GUI or extra Python dependencies.

Usage: python3 tests/render_ass.py subtitles.ass output-directory --libass /path/to/libass
API: https://github.com/libass/libass/blob/master/libass/ass.h
"""
import argparse
import ctypes as C
import json
from pathlib import Path
import struct
import zlib


class AssImage(C.Structure):
    pass


AssImage._fields_ = [("w", C.c_int), ("h", C.c_int), ("stride", C.c_int),
                    ("bitmap", C.POINTER(C.c_ubyte)), ("color", C.c_uint32),
                    ("dst_x", C.c_int), ("dst_y", C.c_int),
                    ("next", C.POINTER(AssImage)), ("type", C.c_int)]


def load_library(path):
    folder = Path(path).parent
    for name in ("libfreetype.6.dylib", "libfribidi.0.dylib", "libharfbuzz.0.dylib", "libunibreak.6.dylib"):
        if (folder / name).exists():
            C.CDLL(str(folder / name), mode=C.RTLD_GLOBAL)
    lib = C.CDLL(str(path))
    signatures = {
        "ass_library_init": (C.c_void_p, []),
        "ass_library_done": (None, [C.c_void_p]),
        "ass_set_extract_fonts": (None, [C.c_void_p, C.c_int]),
        "ass_renderer_init": (C.c_void_p, [C.c_void_p]),
        "ass_renderer_done": (None, [C.c_void_p]),
        "ass_set_frame_size": (None, [C.c_void_p, C.c_int, C.c_int]),
        "ass_set_storage_size": (None, [C.c_void_p, C.c_int, C.c_int]),
        "ass_set_fonts": (None, [C.c_void_p, C.c_char_p, C.c_char_p, C.c_int, C.c_char_p, C.c_int]),
        "ass_add_font": (None, [C.c_void_p, C.c_char_p, C.c_char_p, C.c_int]),
        "ass_read_file": (C.c_void_p, [C.c_void_p, C.c_char_p, C.c_char_p]),
        "ass_free_track": (None, [C.c_void_p]),
        "ass_render_frame": (C.POINTER(AssImage), [C.c_void_p, C.c_void_p, C.c_longlong, C.POINTER(C.c_int)]),
    }
    for name, (result, args) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, args
    return lib


def write_png(path, rgb, width, height):
    def chunk(name, value):
        return struct.pack(">I", len(value)) + name + value + struct.pack(">I", zlib.crc32(name + value))
    rows = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def render(ass_file, output, library_path, times, font=None):
    lib = load_library(library_path)
    handle = lib.ass_library_init()
    if not handle:
        raise RuntimeError("libass initialization failed")
    renderer, track = None, None
    try:
        lib.ass_set_extract_fonts(handle, 1)
        if font:
            data = Path(font).read_bytes()
            lib.ass_add_font(handle, Path(font).name.encode(), data, len(data))
        renderer = lib.ass_renderer_init(handle)
        if not renderer:
            raise RuntimeError("libass renderer initialization failed")
        width, height = 960, 540
        lib.ass_set_storage_size(renderer, 1920, 1080)
        lib.ass_set_frame_size(renderer, width, height)
        lib.ass_set_fonts(renderer, None, b"Nanum Gothic", 1, None, 0)
        track = lib.ass_read_file(handle, str(Path(ass_file).resolve()).encode(), b"UTF-8")
        if not track:
            raise RuntimeError("libass could not read subtitle file")
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        reports = []
        for seconds in times:
            rgb = bytearray([24] * (width * height * 3))
            changed = C.c_int()
            image = lib.ass_render_frame(renderer, track, round(seconds * 1000), C.byref(changed))
            components, pixels, boxes = 0, 0, []
            while image:
                item = image.contents
                color = [(item.color >> shift) & 255 for shift in (24, 16, 8)]
                opacity = 255 - (item.color & 255)
                if item.w and item.h and opacity:
                    components += 1
                    boxes.append([item.dst_x, item.dst_y, item.w, item.h])
                    for y in range(item.h):
                        row = C.string_at(C.addressof(item.bitmap.contents) + y * item.stride, item.w)
                        for x, mask in enumerate(row):
                            alpha = mask * opacity // 255
                            dx, dy = item.dst_x + x, item.dst_y + y
                            if alpha and 0 <= dx < width and 0 <= dy < height:
                                pixels += 1
                                pos = (dy * width + dx) * 3
                                for c in range(3):
                                    rgb[pos + c] = (color[c] * alpha + rgb[pos + c] * (255 - alpha)) // 255
                image = item.next
            if not pixels:
                raise RuntimeError(f"No subtitle pixels at {seconds}s")
            name = f"subtitle-{seconds:08.3f}.png"
            write_png(output / name, rgb, width, height)
            reports.append({"seconds": seconds, "rendered_components": components,
                            "painted_pixels": pixels, "boxes": boxes, "image": name})
        (output / "render-report.json").write_text(json.dumps(reports, indent=2) + "\n")
        return reports
    finally:
        if track:
            lib.ass_free_track(track)
        if renderer:
            lib.ass_renderer_done(renderer)
        lib.ass_library_done(handle)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ass_file")
    parser.add_argument("output")
    parser.add_argument("--libass", required=True)
    parser.add_argument("--font")
    parser.add_argument("--times", type=float, nargs="+", required=True)
    args = parser.parse_args()
    print(json.dumps(render(args.ass_file, args.output, args.libass, args.times, args.font), indent=2))
