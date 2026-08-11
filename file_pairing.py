from __future__ import annotations

from pathlib import Path


def is_capture_complete(folder: Path, index: int, scope_enabled: bool) -> bool:
    stem = f"{index:06d}"
    csv_exists = (folder / f"{stem}.csv").is_file()
    if not scope_enabled:
        return csv_exists
    return csv_exists and (folder / f"{stem}.npz").is_file()


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
    """Remove an orphan CSV or NPZ so retrying an index starts from a clean pair."""
    stem = f"{index:06d}"
    csv_path = folder / f"{stem}.csv"
    npz_path = folder / f"{stem}.npz"
    if csv_path.exists() != npz_path.exists():
        csv_path.unlink(missing_ok=True)
        npz_path.unlink(missing_ok=True)
