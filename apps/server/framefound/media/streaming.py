"""Byte-range file streaming for video seek support.

Implements single-range requests (RFC 9110 §14) — what browsers actually send
for <video>. Multi-range requests fall back to the full file. Files stream in
chunks; nothing is loaded whole into memory.
"""

import re
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

CHUNK_BYTES = 512 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


async def _stream(path: Path, start: int, length: int) -> AsyncIterator[bytes]:
    remaining = length
    async with await anyio.open_file(path, "rb") as fh:
        await fh.seek(start)
        while remaining > 0:
            chunk = await fh.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def range_file_response(request: Request, path: Path, content_type: str) -> Response:
    size = path.stat().st_size
    header = request.headers.get("range", "")
    match = _RANGE_RE.match(header) if header else None

    if match is None:
        return StreamingResponse(
            _stream(path, 0, size),
            media_type=content_type,
            headers={"Content-Length": str(size), "Accept-Ranges": "bytes"},
        )

    start_s, end_s = match.groups()
    if start_s == "" and end_s == "":
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    if start_s == "":  # suffix form: last N bytes
        length = min(int(end_s), size)
        start, end = size - length, size - 1
    else:
        start = int(start_s)
        end = min(int(end_s), size - 1) if end_s else size - 1
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    length = end - start + 1
    return StreamingResponse(
        _stream(path, start, length),
        status_code=206,
        media_type=content_type,
        headers={
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
        },
    )
