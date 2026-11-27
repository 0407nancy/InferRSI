"""Retain interrupted local preparation evidence before a supervised retry."""
from pathlib import Path
import tempfile

from .common import LoopError


def archive_incomplete(directory, completion_file):
    """Move only incomplete local preparation; caller holds the driver lock."""
    directory = Path(directory)
    if Path(completion_file).name != completion_file:
        raise LoopError("Completion marker must be a filename")
    if directory.is_symlink():
        raise LoopError("Refusing to archive a symlinked preparation directory")
    if not directory.exists() or (directory / completion_file).exists():
        return None
    if not directory.is_dir():
        raise LoopError("Preparation path is not a directory")
    archive = Path(tempfile.mkdtemp(prefix=directory.name + "-incomplete-", dir=directory.parent))
    destination = archive / directory.name
    directory.rename(destination)
    return destination
