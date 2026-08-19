from __future__ import annotations

from pathlib import Path


def is_capture_complete(folder: Path, index: int, scope_enabled: bool) -> bool:
    stem = f"{index:06d}"
    csv_exists = (folder / f"{stem}.csv").is_file()
    if not scope_enabled:
        return csv_exists
    return all(
        path.is_file()
        for path in (
            folder / f"{stem}.csv",
            folder / f"{stem}_delay.npz",
            folder / f"{stem}_cycles.npz",
        )
    )


def next_capture_index(folder: Path, scope_enabled: bool) -> int:
    """Return the next index while preserving legacy CSV-only behavior."""
    if not folder.exists():
        return 1

    if not scope_enabled:
        indices: list[int] = []
        for csv_path in folder.glob("*.csv"):
            try:
                indices.append(int(csv_path.stem))
            except ValueError:
                continue
        return max(indices, default=0) + 1

    index = 1
    while is_capture_complete(folder, index, scope_enabled=True):
        index += 1
    return index


def remove_incomplete_pair(folder: Path, index: int) -> None:
    """Compatibility name: remove an incomplete new three-file capture group."""
    remove_incomplete_group(folder, index)


def remove_incomplete_group(folder: Path, index: int) -> None:
    """Remove partial formal files so retrying an index starts from a clean group."""
    stem = f"{index:06d}"
    paths = (
        folder / f"{stem}.csv",
        folder / f"{stem}_delay.npz",
        folder / f"{stem}_cycles.npz",
    )
    present = sum(path.is_file() for path in paths)
    if 0 < present < len(paths):
        for path in paths:
            path.unlink(missing_ok=True)
