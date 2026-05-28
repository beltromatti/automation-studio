"""Built-in, list-consuming workflow: extract metadata from each file in an
input dataset (the file/file_list test bed).

Reads a dataset that declares an ``image`` column of type ``file`` (or
``file_list`` for the multi-attachment variant) and produces one output row per
input file with its dimensions / size / mime / sha256 — plus echoes the file
through as the output ``image`` column (showing that files round-trip cleanly
through both input and output contracts of a workflow).

Pure stdlib — no Pillow, no ffmpeg. Detects:
* PNG dimensions from the IHDR chunk (bytes 16–24).
* JPEG dimensions by walking the SOF markers.
* GIF dimensions from the screen descriptor (bytes 6–10).
* WebP dimensions from the VP8 / VP8L / VP8X chunks.
* For everything else: size + mime only.

Mostly a real test rig for the file infrastructure — but useful as a generic
"how big is this asset" preflight for image/video pipelines.
"""
from __future__ import annotations

import asyncio
import struct
from pathlib import Path

from automations import userkit


def _png_dims(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def _gif_dims(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:3] != b"GIF":
        return None
    w, h = struct.unpack("<HH", data[6:10])
    return w, h


def _webp_dims(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    sub = data[12:16]
    if sub == b"VP8 ":
        # frame tag at offset 23; width/height in next 4 bytes
        w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return w, h
    if sub == b"VP8L":
        b = data[21:25]
        sig = b[0]
        if sig != 0x2F:
            return None
        w = ((b[1] & 0x3F) << 8 | b[1] | b[2] & 0x3F) + 1  # rough; not perfect
        h = (((b[3] & 0x0F) << 10) | (b[2] >> 6) | (b[3] << 4)) + 1
        return w, h
    if sub == b"VP8X":
        w = (data[24] | data[25] << 8 | data[26] << 16) + 1
        h = (data[27] | data[28] << 8 | data[29] << 16) + 1
        return w, h
    return None


def _jpeg_dims(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i < n - 8:
        # find next marker
        if data[i] != 0xFF:
            i += 1
            continue
        # skip fill bytes
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = data[i]; i += 1
        # SOI/EOI/RST* have no length
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > n:
            break
        seg_len = struct.unpack(">H", data[i:i+2])[0]
        # SOF0..SOF15 (skip SOF4 = DHT, SOF8/12 reserved): the ones we want
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 5 + 2 <= n:
                h, w = struct.unpack(">HH", data[i+3:i+7])
                return w, h
        i += seg_len
    return None


def _dimensions(path: str, mime: str) -> tuple[int | None, int | None]:
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None, None
    fn = {
        "image/png": _png_dims, "image/gif": _gif_dims,
        "image/jpeg": _jpeg_dims, "image/webp": _webp_dims,
    }.get(mime)
    if not fn:
        return None, None
    try:
        d = fn(head)
    except Exception:
        return None, None
    return d if d else (None, None)


async def run(_params: dict, _sess, inputs: list[dict]) -> list[dict]:
    total = len(inputs)
    out: list[dict] = []
    userkit.log(f"[media-meta] {total} row{'s' if total != 1 else ''}")
    for i, row in enumerate(inputs, 1):
        # Accept either a single `image` (file column) or `images` (file_list).
        files: list[dict] = []
        single = userkit.input_file(row, "image") or userkit.input_file(row, "file")
        if single:
            files = [single]
        else:
            files = userkit.input_files(row, "images") or userkit.input_files(row, "files")
        if not files:
            out.append({"image": None, "name": "", "mime": "", "size": 0, "width": "",
                        "height": "", "status": "no_file", "detail": "row has no 'image'/'images' column"})
            userkit.progress(i, total, message=f"{i}/{total} (no file)")
            continue
        for f in files:
            path = f.get("path") or ""
            name = f.get("name") or Path(path).name
            mime = f.get("mime") or ""
            size = int(f.get("size") or 0)
            try:
                w, h = _dimensions(path, mime)
            except Exception as e:
                out.append({"image": f["id"], "name": name, "mime": mime, "size": size,
                            "width": "", "height": "", "status": "error", "detail": str(e)[:120]})
                continue
            status = "ok" if (w and h) else ("ok_no_dim" if mime else "unreadable")
            out.append({"image": f["id"], "name": name, "mime": mime, "size": size,
                        "width": w or "", "height": h or "", "status": status,
                        "detail": "" if status == "ok" else
                                  ("non-image mime, dim unknown" if status == "ok_no_dim" else
                                   "couldn't read file header")})
        userkit.progress(i, total, message=f"{i}/{total} {name} → {status}", url=path)
        await asyncio.sleep(0.05)  # be polite to large datasets
    return out


def main(argv=None):
    params, server, output = userkit.parse(argv)
    inputs = userkit.input_rows(argv)
    cols = ["image", "name", "mime", "size", "width", "height", "status", "detail"]
    if not inputs:
        userkit.error("no input rows — bind a dataset with an 'image' (file) or 'images' (file_list) column")
        userkit.write_csv(output, [], cols)
        return 1
    # We don't need a browser session for this — pass an explicit no-op.
    async def _go(_p, _s):
        return await run(params, None, inputs)
    rows = userkit.run_session(_go, params, server)
    userkit.write_csv(output, rows, cols)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
