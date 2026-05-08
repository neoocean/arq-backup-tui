"""Restore Arq 7 backups produced by ``arq_writer`` (and any other
backup that conforms to the same on-disk standalone-objects layout).

The reader is the inverse of ``arq_writer``: every encoder there has
a decoder here. Combined, they form a closed self-hosting round-trip
that doubles as a byte-level compatibility check (``arq_writer`` ->
``arq_reader`` -> compare bytes against the original source tree).

High-level API:

    from arq_reader import Restore
    restore = Restore(
        src=Path("/path/to/backup-dest"),
        encryption_password="...",
    )
    restore.list_folders()                # discover backup folders
    restore.restore(folder_uuid=..., dest=Path("/path/out"))

CLI: ``python -m arq_reader list <src> --password ...``,
      ``python -m arq_reader restore <src> <folder-uuid> <dest> --password ...``.
"""

from .decrypt import (
    DecryptError,
    decrypt_encrypted_object,
    decrypt_lz4_arqo,
)
from .parse import (
    BinaryReader,
    parse_blobloc,
    parse_node,
    parse_tree,
)
from .restore import Restore, RestoreResult

__all__ = [
    "Restore",
    "RestoreResult",
    "DecryptError",
    "decrypt_encrypted_object",
    "decrypt_lz4_arqo",
    "BinaryReader",
    "parse_blobloc",
    "parse_node",
    "parse_tree",
]

__version__ = "0.1.0"
