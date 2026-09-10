"""Small shared helpers for the local, non-adversarial model evaluations."""
from pathlib import Path


def resolve_tool_path(directory: Path, file_path: str) -> Path:
    """Resolve actor-relative paths against its working directory, not ours."""
    path = Path(file_path)
    return (path if path.is_absolute() else directory / path).resolve()
