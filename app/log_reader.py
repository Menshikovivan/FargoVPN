"""Read a bounded UTF-8 log suffix without scanning the entire file."""
from pathlib import Path


def tail_lines(path: Path, limit: int = 500, max_bytes: int = 2 * 1024 * 1024) -> tuple[list[str], bool]:
    limit = max(1, min(int(limit), 2000))
    chunks = []
    read_bytes = 0
    with Path(path).open('rb') as handle:
        handle.seek(0, 2)
        position = handle.tell()
        newlines = 0
        while position > 0 and read_bytes < max_bytes and newlines <= limit:
            size = min(65536, position, max_bytes - read_bytes)
            position -= size
            handle.seek(position)
            block = handle.read(size)
            chunks.append(block)
            read_bytes += len(block)
            newlines += block.count(b'\n')
    data = b''.join(reversed(chunks))
    # The first line can be partial after either a seek or the byte cap.
    if position > 0:
        first = data.find(b'\n')
        if first >= 0:
            data = data[first + 1:]
    return data.decode('utf-8', errors='replace').splitlines(keepends=True)[-limit:], position > 0 and read_bytes >= max_bytes
