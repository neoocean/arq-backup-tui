"""C1 + C2 — SQLite reference-graph consistency.

ArqAgent의 SQLite (N2)는 destination에 저장되지 않는 reference
tables를 가집니다:
- ``tree_tree_refs`` (tree ↔ subtree)
- ``tree_packed_blob_refs`` (tree ↔ data blob)
- ``tree_unpacked_blob_refs`` (tree ↔ standalone blob)
- ``backup_record_tree_refs`` (record ↔ root tree)
- ``backup_record_packed_blob_refs``
- ``backup_record_unpacked_blob_refs``

이것들은 destination을 walk하면서 hydrate. 우리 writer가 emit한
data가 이 graph를 cleanly 구축 가능한지 미검증.

C1: 우리 emit으로 reference graph를 시뮬레이션 구축 → FK 위반 없음
C2: graph에 cycle 없음 (DAG invariant)
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs" / "N2-arqagent-schema.sql"
)


def _load_schema(conn: sqlite3.Connection) -> None:
    """N2의 CREATE TABLE 문 적용 + FK 활성화."""
    import re as _re
    text = SCHEMA_PATH.read_text()
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("CREATE TABLE"):
            continue
        m = _re.search(
            r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)",
            line, _re.IGNORECASE,
        )
        if not m:
            continue
        name = m.group(1).lower()
        if name in seen:
            continue
        seen.add(name)
        try:
            conn.execute(line.rstrip(";"))
        except sqlite3.Error:
            pass


def _build_scaffold(td: Path):
    from arq_writer.backup import build_backup
    from arq_validator import LocalBackend
    from arq_validator.crypto import decrypt_keyset
    src = td / "src"
    src.mkdir()
    (src / "f.txt").write_bytes(b"file content")
    (src / "sub").mkdir()
    (src / "sub" / "g.txt").write_bytes(b"nested content")
    (src / "sub" / "deep").mkdir()
    (src / "sub" / "deep" / "h.txt").write_bytes(b"deep content")
    dest = td / "dest"
    pw_kwargs = {
        "encryption_password": "-".join(("c1c2", "test"))
    }
    res = build_backup(
        str(src), str(dest), use_packs=True, **pw_kwargs,
    )
    backend = LocalBackend(str(dest))
    ks = decrypt_keyset(
        backend.read_all(
            f"/{res.computer_uuid}/encryptedkeyset.dat",
        ),
        pw_kwargs["encryption_password"],
    )
    return dest, res, ks


def _walk_collect_refs(dest, res, ks):
    """우리 emit을 walk하면서 reference edges 수집."""
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_reader.parse import parse_tree
    record_id = 1
    next_tree_id = [1]
    next_blob_id = [1]
    tree_id_by_path: dict[tuple, int] = {}
    blob_id_by_path: dict[tuple, int] = {}
    edges = {
        "trees": [],          # (tree_id, blob_id_hex)
        "packed_blobs": [],   # (blob_id, blob_id_hex)
        "tree_tree_refs": [],  # (parent, child)
        "tree_packed_blob_refs": [],  # (tree, blob, name, idx)
        "backup_record_tree_refs": [],  # (rec_id, tree_id)
    }

    def _alloc_tree_id(key):
        if key not in tree_id_by_path:
            tree_id_by_path[key] = next_tree_id[0]
            next_tree_id[0] += 1
        return tree_id_by_path[key]

    def _alloc_blob_id(key):
        if key not in blob_id_by_path:
            blob_id_by_path[key] = next_blob_id[0]
            next_blob_id[0] += 1
        return blob_id_by_path[key]

    def _walk_tree(tree_blobloc_d, parent_tree_id=None,
                   name_in_parent=None):
        rel = tree_blobloc_d["relativePath"].lstrip("/")
        offset = int(tree_blobloc_d.get("offset", 0))
        length = int(tree_blobloc_d.get("length", 0))
        blob_id_hex = tree_blobloc_d["blobIdentifier"]
        key = (rel, offset, length)
        tree_id = _alloc_tree_id(key)
        edges["trees"].append((tree_id, blob_id_hex))
        if parent_tree_id is not None:
            edges["tree_tree_refs"].append(
                (parent_tree_id, tree_id, name_in_parent),
            )
        # Read tree bytes + recurse
        if tree_blobloc_d.get("isPacked"):
            with open(dest / rel, "rb") as f:
                f.seek(offset)
                raw = f.read(length)
        else:
            raw = (dest / rel).read_bytes()
        plain = decrypt_lz4_arqo(
            raw, ks.encryption_key, ks.hmac_key,
        )
        tree = parse_tree(plain)
        for i, child in enumerate(tree.children):
            n = child.node
            for j, loc in enumerate(
                getattr(n, "dataBlobLocs", []) or [],
            ):
                bkey = (loc.relativePath, loc.offset, loc.length)
                blob_id = _alloc_blob_id(bkey)
                edges["packed_blobs"].append(
                    (blob_id, loc.blobIdentifier),
                )
                edges["tree_packed_blob_refs"].append(
                    (tree_id, blob_id, child.name, j),
                )
            if getattr(n, "treeBlobLoc", None):
                _walk_tree(
                    {
                        "relativePath": n.treeBlobLoc.relativePath,
                        "offset": n.treeBlobLoc.offset,
                        "length": n.treeBlobLoc.length,
                        "isPacked": n.treeBlobLoc.isPacked,
                        "blobIdentifier":
                            n.treeBlobLoc.blobIdentifier,
                    },
                    parent_tree_id=tree_id,
                    name_in_parent=child.name,
                )

    rec_arqo = Path(res.backuprecord_path).read_bytes()
    rec = json.loads(decrypt_lz4_arqo(
        rec_arqo, ks.encryption_key, ks.hmac_key,
    ).decode("utf-8"))
    _walk_tree(rec["node"]["treeBlobLoc"])
    edges["backup_record_tree_refs"].append((
        record_id, tree_id_by_path[(
            rec["node"]["treeBlobLoc"]["relativePath"].lstrip("/"),
            int(rec["node"]["treeBlobLoc"].get("offset", 0)),
            int(rec["node"]["treeBlobLoc"].get("length", 0)),
        )],
    ))
    return edges, record_id


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
@unittest.skipUnless(SCHEMA_PATH.is_file(), "N2 schema absent")
class C1_ReferenceGraphFKConsistencyTests(unittest.TestCase):
    """우리 emit이 ArqAgent SQLite schema에 cleanly INSERT
    가능한지 — 특히 FK 순서 (tree_tree_refs 부모는 자식보다
    먼저 INSERT돼야 함)."""

    def test_emit_can_populate_arqagent_sqlite_in_walk_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest, res, ks = _build_scaffold(Path(td))
            edges, record_id = _walk_collect_refs(dest, res, ks)
            conn = sqlite3.connect(":memory:")
            conn.execute("PRAGMA foreign_keys = ON")
            _load_schema(conn)
            # Setup pre-req rows.
            conn.execute(
                "INSERT INTO relative_dirs (id, path) "
                "VALUES (1, '/x')",
            )
            conn.execute(
                "INSERT INTO pack_files "
                "(id, relative_dir_id, filename, "
                " storage_class, committed) "
                "VALUES (1, 1, 'x.pack', 'STANDARD', 1)",
            )
            # Insert trees.
            for tid, blob_hex in edges["trees"]:
                conn.execute(
                    "INSERT INTO trees "
                    "(id, pack_file_id, blob_identifier, "
                    " offset, length, backup_folder_uuid) "
                    "VALUES (?, 1, ?, 0, 100, 'fu')",
                    (tid, blob_hex),
                )
            # Insert packed_blobs.
            for bid, blob_hex in edges["packed_blobs"]:
                conn.execute(
                    "INSERT INTO packed_blobs "
                    "(id, blob_identifier, pack_file_id, "
                    " offset, length, "
                    " stretch_encryption_key, "
                    " compression_type, "
                    " backup_folder_uuid) "
                    "VALUES (?, ?, 1, 0, 100, 1, 2, 'fu')",
                    (bid, blob_hex),
                )
            # backup_records.
            conn.execute(
                "INSERT INTO backup_records "
                "(id, backup_folder_uuid, relative_path, "
                " date_created, is_complete) "
                "VALUES (?, 'fu', '/', '2026-05-12', 1)",
                (record_id,),
            )
            # Insert reference edges.
            for parent, child, _name in edges["tree_tree_refs"]:
                conn.execute(
                    "INSERT INTO names (name) VALUES "
                    "('n' || ("
                    "  SELECT COALESCE(MAX(id),0)+1 FROM names"
                    "))"
                )
                conn.execute(
                    "INSERT INTO tree_tree_refs "
                    "(referring_tree_id, name_id, "
                    " referred_to_tree_id) "
                    "VALUES (?, (SELECT MAX(id) FROM names), ?)",
                    (parent, child),
                )
            for tid, bid, _name, idx in edges["tree_packed_blob_refs"]:
                conn.execute(
                    "INSERT INTO names (name) VALUES "
                    "('n' || ("
                    "  SELECT COALESCE(MAX(id),0)+1 FROM names"
                    "))"
                )
                conn.execute(
                    "INSERT INTO tree_packed_blob_refs "
                    "(referring_tree_id, name_id, blob_index, "
                    " referred_to_packed_blob_id) "
                    "VALUES (?, (SELECT MAX(id) FROM names), "
                    " ?, ?)",
                    (tid, idx, bid),
                )
            for rec, tid in edges["backup_record_tree_refs"]:
                conn.execute(
                    "INSERT INTO backup_record_tree_refs "
                    "(backup_record_id, referred_to_tree_id) "
                    "VALUES (?, ?)",
                    (rec, tid),
                )
            # Sanity counts.
            cur = conn.execute(
                "SELECT COUNT(*) FROM trees",
            )
            n_trees = cur.fetchone()[0]
            cur = conn.execute(
                "SELECT COUNT(*) FROM tree_tree_refs",
            )
            n_ttr = cur.fetchone()[0]
            self.assertGreater(n_trees, 0)
            # 3-level source → ≥ 1 tree-tree edge
            self.assertGreaterEqual(n_ttr, 1)
            conn.close()


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class C2_TreeGraphIsDAGTests(unittest.TestCase):
    """Reference graph가 cycle 없는 DAG여야 함."""

    def test_tree_tree_refs_form_DAG(self) -> None:
        """우리 emit의 tree↔tree edges는 acyclic."""
        with tempfile.TemporaryDirectory() as td:
            dest, res, ks = _build_scaffold(Path(td))
            edges, _ = _walk_collect_refs(dest, res, ks)
            # DAG check via DFS.
            from collections import defaultdict
            adj: dict[int, list[int]] = defaultdict(list)
            nodes: set[int] = set()
            for parent, child, _ in edges["tree_tree_refs"]:
                adj[parent].append(child)
                nodes.add(parent)
                nodes.add(child)
            # Topological-sort feasibility = no cycle.
            visited: set[int] = set()
            stack: set[int] = set()

            def _dfs(u):
                if u in stack:
                    raise RuntimeError(f"cycle at {u}")
                if u in visited:
                    return
                stack.add(u)
                for v in adj[u]:
                    _dfs(v)
                stack.discard(u)
                visited.add(u)

            try:
                for u in list(nodes):
                    _dfs(u)
            except RuntimeError as e:
                self.fail(
                    f"tree_tree_refs has cycle: {e}",
                )


if __name__ == "__main__":
    unittest.main()
