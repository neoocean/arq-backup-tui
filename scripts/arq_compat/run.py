#!/usr/bin/env python3
"""Arq GUI round-trip compatibility suite — orchestrator.

Re-runnable every time the installed Arq.app version changes; tests many
scenarios in BOTH directions and accumulates per-version results under
``docs/arq-compat/``.

Automation boundary (``arqc`` has no restore command, so Arq's *read* of
our output cannot be CLI-driven):

  Direction A  our writer -> Arq          [SEMI-AUTO]
    auto:  our reader round-trip (writer emit must be self-restorable)
    auto:  patched arq_restore proxy (independent Arq-spec reader) [opt]
    manual: real Arq.app GUI restore + `confirm-gui-restore` diff
  Direction B  Arq -> our reader          [AUTO, needs one-time plan]
    `arqc startBackupPlan` + poll `latestBackupActivityJSON`, then our
    reader restores Arq's destination + `--verify-after` + per-scenario diff
  Format drift                            [AUTO]
    fingerprint + schema-validate Arq's emit; diff vs the previous
    version's stored baseline

One-time setup is described in ``docs/arq-compat/README.md``.

Subcommands::

    run.py all       [--arq-dest .. --plan-uuid .. --computer-uuid .. --skip-backup]
                     # idempotent: no-ops if this Arq version already has a
                     # report (runs/<version>_*.md); --force to re-run.
    run.py direction-a --workdir DIR
    run.py direction-b --workdir DIR --plan-uuid U --arq-dest D --arq-pw P
    run.py confirm-gui-restore --workdir DIR --restored DIR
    run.py baseline  --arq-dest D --arq-pw P     # capture/drift only
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DOCS = REPO / "docs" / "arq-compat"
RUNS = DOCS / "runs"
BASELINES = DOCS / "baselines"
ARQC = "/Applications/Arq.app/Contents/Resources/arqc"
ARQ_APP = "/Applications/Arq.app"
SERVER_DB = "/Library/Application Support/ArqAgent/server.db"

sys.path.insert(0, str(HERE))
import scenarios as scen  # noqa: E402


# --- helpers ---------------------------------------------------------------

def arq_version() -> str:
    try:
        out = subprocess.run(
            ["defaults", "read",
             f"{ARQ_APP}/Contents/Info.plist", "CFBundleShortVersionString"],
            capture_output=True, text=True, timeout=10,
        )
        v = out.stdout.strip()
        return v or "unknown"
    except Exception:
        return "unknown"


# --- Arq agent activity / schema introspection (read-only) -----------------

def _server_db_query(sql: str, params=()):
    """Run a read-only query against a *copy* of Arq's server.db.

    server.db is root-owned + daemon-owned; we never open the live file for
    writing or hold a lock on it. Copy it (world-readable) to a temp path and
    query the copy, so there is zero contention with ArqAgent. Returns a list
    of sqlite3.Row, or [] if the DB/columns are unavailable.
    """
    import shutil
    import sqlite3
    import tempfile
    src = Path(SERVER_DB)
    if not src.exists():
        return []
    tmp = Path(tempfile.mkdtemp()) / "server.db"
    try:
        shutil.copy2(src, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    finally:
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass


def latest_arq_activity(plan_uuid: str, sub_types=("backup",)) -> Optional[Dict]:
    """Latest `activities` row for a plan (Arq's own ground truth for a backup
    or restore: finished_time / aborted / error_count / counts / log path)."""
    placeholders = ",".join("?" for _ in sub_types)
    rows = _server_db_query(
        "SELECT type, sub_type, active, finished_time, aborted, abort_reason, "
        "error_count, processed_bytes, total_bytes, processed_files, "
        "total_files, restore_destination, restore_option, activity_log_path "
        f"FROM activities WHERE plan_uuid=? AND sub_type IN ({placeholders}) "
        "ORDER BY id DESC LIMIT 1",
        (plan_uuid, *sub_types),
    )
    if not rows:
        return None
    r = rows[0]
    return {k: r[k] for k in r.keys()}


def arqc_activity(plan_uuid: str) -> Dict:
    """Parse `arqc latestBackupActivityJSON` (clean JSON: finishedTime,
    aborted, errorCount, maxErrorSeverity, message, counts)."""
    out = _arqc("latestBackupActivityJSON", plan_uuid)
    txt = out.stdout or ""
    i = txt.find("{")
    if i < 0:
        return {}
    try:
        return json.loads(txt[i:])
    except Exception:
        return {}


def arq_db_schema_fingerprint() -> Dict[str, List[str]]:
    """Column list of Arq's key config/activity tables — a drift sentinel for
    the local server.db schema (which is unversioned and changes silently
    across Arq versions; see docs/RESEARCH-shared-arq-config-feasibility.md)."""
    fp: Dict[str, List[str]] = {}
    for tbl in ("backup_plans", "storage_locations", "activities"):
        rows = _server_db_query(f"PRAGMA table_info({tbl})")
        fp[tbl] = [f"{r['name']}:{r['type']}" for r in rows]
    return fp


def _notify(title: str, message: str) -> None:
    """Best-effort macOS notification (used by --notify on FAIL/DRIFT)."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(message)} '
             f'with title {json.dumps(title)}'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


PW_ENV = "_ARQ_COMPAT_PW"  # passwords go via env, never on the CLI


def _py(*args: str, pw_env: Optional[str] = None,
        **kw) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS", "1")
    env.setdefault("ARQ_TUI_SKIP_DISK_PRECHECK", "1")
    # Never pass the encryption secret as a CLI argument — it would be
    # visible in `ps`/process listings. The reader/writer/fingerprint CLIs
    # all accept ``--password-env``; we hand the value through the
    # subprocess environment under PW_ENV instead.
    if pw_env is not None:
        env[PW_ENV] = pw_env
    return subprocess.run(
        [sys.executable, "-m", *args], cwd=str(REPO), env=env,
        capture_output=True, text=True, **kw,
    )


def _sha(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _index(root: Path) -> Dict[str, Path]:
    """Map source-relative POSIX path -> file path, for regular files."""
    out: Dict[str, Path] = {}
    for p in root.rglob("*"):
        if p.is_symlink():
            out["@symlink:" + str(p.relative_to(root))] = p
        elif p.is_file():
            out[str(p.relative_to(root))] = p
    return out


def diff_scenario(fix_dir: Path, res_dir: Path) -> Tuple[str, str]:
    """Compare one scenario's fixture vs restored subtree.

    Returns (status, detail). status in {PASS, PASS_NORM, FAIL, EMPTY}.
    PASS_NORM = content identical but a filename differed only by NFC/NFD.
    """
    if not res_dir.exists():
        return "FAIL", "restored subdir missing"
    fix = _index(fix_dir)
    res = _index(res_dir)
    if not fix:
        return "EMPTY", "no files in fixture"
    norm_used = False
    res_by_nfc = {unicodedata.normalize("NFC", k): v for k, v in res.items()}
    missing: List[str] = []
    mismatch: List[str] = []
    for rel, fp in fix.items():
        rp = res.get(rel)
        if rp is None:
            rp = res_by_nfc.get(unicodedata.normalize("NFC", rel))
            if rp is not None:
                norm_used = True
        if rp is None:
            missing.append(rel)
            continue
        if rel.startswith("@symlink:"):
            try:
                if os.readlink(fp) != os.readlink(rp):
                    mismatch.append(rel + " (link target)")
            except OSError:
                mismatch.append(rel + " (readlink)")
            continue
        try:
            if _sha(fp) != _sha(rp):
                mismatch.append(rel)
        except OSError as e:
            mismatch.append(f"{rel} ({e})")
    if missing or mismatch:
        bits = []
        if missing:
            bits.append(f"missing={len(missing)}:{missing[:3]}")
        if mismatch:
            bits.append(f"mismatch={len(mismatch)}:{mismatch[:3]}")
        return "FAIL", "; ".join(bits)
    return ("PASS_NORM" if norm_used else "PASS",
            f"{len(fix)} files OK" + (" (filename NFC/NFD normalised)"
                                      if norm_used else ""))


def diff_all(fixtures: Path, restored_base: Path) -> Dict[str, Dict[str, str]]:
    """Per-scenario diff. ``restored_base`` mirrors the fixture layout."""
    res: Dict[str, Dict[str, str]] = {}
    for sc in scen.SCENARIOS:
        st, detail = diff_scenario(fixtures / sc.name, restored_base / sc.name)
        res[sc.name] = {"status": st, "detail": detail}
    return res


# --- Direction A: our writer -> (our reader | arq_restore | Arq GUI) -------

WRITER_CONFIGS = [
    ("v4-buzhash", ["--tree-version", "4", "--chunker", "arq_v7_41"]),
    ("v4-fixed", ["--tree-version", "4", "--chunker", "fixed-40m"]),
    ("v3-buzhash", ["--tree-version", "3", "--chunker", "arq_v7_41"]),
]


def direction_a(workdir: Path, arq_restore_bin: Optional[str]) -> Dict:
    import secrets as _secrets
    fixtures = workdir / "fixtures"
    notes = scen.generate(fixtures)
    # Throwaway encryption password for the disposable Direction-A
    # destinations — generated per run (never hardcoded / committed). It is
    # surfaced on stdout so the operator can unlock the destination in
    # Arq.app for the manual GUI-restore leg.
    gui_pw = _secrets.token_urlsafe(18)
    result: Dict = {"fixture_notes": notes, "configs": {},
                    "gui_pw": gui_pw}
    for cfg_name, cfg_args in WRITER_CONFIGS:
        wdest = workdir / f"writer_{cfg_name}"
        if wdest.exists():
            shutil.rmtree(wdest)
        wdest.mkdir(parents=True)
        w = _py("arq_writer.cli", "create", str(fixtures), "--dest",
                str(wdest), "--password-env", PW_ENV, "--use-packs",
                "--backup-name", "compat", "--folder-name", "corpus",
                *cfg_args, pw_env=gui_pw)
        cfg: Dict = {"writer_ok": w.returncode == 0}
        if w.returncode != 0:
            cfg["writer_err"] = w.stderr[-500:]
            result["configs"][cfg_name] = cfg
            continue
        try:
            wjson = json.loads(w.stdout[w.stdout.index("{"):])
            folder_uuid = wjson["folder_uuid"]
        except Exception as e:
            cfg["writer_err"] = f"parse folder_uuid: {e}"
            result["configs"][cfg_name] = cfg
            continue
        # our reader round-trip
        rdest = workdir / f"reader_{cfg_name}"
        if rdest.exists():
            shutil.rmtree(rdest)
        rdest.mkdir(parents=True)
        r = _py("arq_reader.cli", "restore", str(wdest), folder_uuid,
                str(rdest), "--password-env", PW_ENV, "--verify-after",
                pw_env=gui_pw)
        cfg["reader_exit"] = r.returncode
        # The per-scenario content diff (SHA-256) is the authoritative
        # round-trip verdict; capture the reader's own summary too so a
        # non-zero exit is explained (e.g. aggregate size accounting or a
        # macOS read-only-file xattr-apply warning) rather than opaque.
        try:
            rj = json.loads(r.stdout[r.stdout.index("{"):])
            cfg["restore_failures"] = rj.get("failures", [])
            cfg["verify"] = rj.get("verify", {})
        except Exception:
            cfg["verify"] = {"note": "summary parse failed"}
        cfg["reader_scenarios"] = diff_all(fixtures, rdest)
        cfg["dest"] = str(wdest)
        cfg["folder_uuid"] = folder_uuid
        # patched arq_restore proxy (optional, independent Arq-spec reader)
        if arq_restore_bin and Path(arq_restore_bin).exists():
            cfg["arq_restore_proxy"] = "available (run verify.py manually)"
        result["configs"][cfg_name] = cfg
    return result


def confirm_gui_restore(workdir: Path, restored: Path,
                        plan_uuid: Optional[str] = None) -> Dict:
    fixtures = workdir / "fixtures"
    if not fixtures.exists():
        return {"error": "no fixtures in workdir; run direction-a first"}
    out: Dict = {"scenarios": diff_all(fixtures, restored),
                 "restored": str(restored)}
    # Corroborate the file-diff with Arq's own record of the GUI restore: the
    # `activities` table logs restore ops (restore_destination / error_count /
    # finished). If the operator passes the plan UUID, attach that evidence so
    # the pass rests on both our diff AND Arq's own success record.
    if plan_uuid:
        ra = latest_arq_activity(plan_uuid, sub_types=("restore",))
        if ra:
            out["arq_restore_activity"] = {
                "finished": bool(ra.get("finished_time")),
                "aborted": ra.get("aborted"),
                "error_count": ra.get("error_count"),
                "restore_destination": ra.get("restore_destination"),
            }
    return out


# --- Direction B: Arq backs up the fixtures -> our reader ------------------

def _arqc(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([ARQC, *args], capture_output=True, text=True,
                          timeout=timeout)


def direction_b(workdir: Path, plan_uuid: str, arq_dest: Path,
                arq_pw: str, computer_uuid: Optional[str] = None,
                skip_backup: bool = False, wait_s: int = 600) -> Dict:
    """Restore the Arq-created backup of the round-trip plan and diff it
    per scenario against workdir/fixtures.

    Unless ``skip_backup``, first triggers a fresh Arq backup via
    ``arqc startBackupPlan`` + polls ``latestBackupActivityJSON``. When the
    operator has already backed up via the GUI, pass ``skip_backup=True``.

    ``computer_uuid`` targets one plan's top-level folder — required when the
    destination is shared with other Arq plans (e.g. the real arqbackup1
    storage location holds both real backups and this round-trip plan, each
    in its own planUUID folder with its own keyset)."""
    fixtures = workdir / "fixtures"
    if not fixtures.exists():
        scen.generate(fixtures)
    out: Dict = {"plan_uuid": plan_uuid, "computer_uuid": computer_uuid}
    if not skip_backup:
        start_act = arqc_activity(plan_uuid)
        start_finished = start_act.get("finishedTime")
        start = _arqc("startBackupPlan", plan_uuid)
        out["start_rc"] = start.returncode
        if start.returncode != 0:
            out["error"] = f"startBackupPlan failed: {start.stderr[-300:]}"
            return out
        # Reliable completion: poll the structured activity JSON until the
        # agent reports a NEW finished backup that is idle (not a stale one).
        deadline = time.time() + wait_s
        done = False
        act: Dict = {}
        while time.time() < deadline:
            time.sleep(5)
            act = arqc_activity(plan_uuid)
            finished = act.get("finishedTime")
            if (act.get("message") == "Idle" and finished
                    and finished != start_finished
                    and not act.get("aborted")):
                done = True
                break
        out["completed"] = done
        out["arq_error_count"] = act.get("errorCount")
        out["arq_max_error_severity"] = act.get("maxErrorSeverity")
        out["arq_aborted"] = act.get("aborted")
    else:
        out["completed"] = "skipped (operator backed up via GUI)"
    # Arq's own ground-truth from server.db `activities` (cross-check + errors)
    act_row = latest_arq_activity(plan_uuid, sub_types=("backup",))
    if act_row:
        out["arq_activity"] = {
            "finished": bool(act_row.get("finished_time")),
            "aborted": act_row.get("aborted"),
            "error_count": act_row.get("error_count"),
            "processed_files": act_row.get("processed_files"),
            "total_files": act_row.get("total_files"),
        }
    # our reader restores Arq's destination (the target plan's folder)
    lst = _py("arq_reader.cli", "list", str(arq_dest), "--password-env",
              PW_ENV, pw_env=arq_pw)
    try:
        ldata = json.loads(lst.stdout)
        comps = ldata["computers"]
        comp = next((c for c in comps if c["computer_uuid"] == computer_uuid),
                    comps[0]) if computer_uuid else comps[0]
        folder = comp["folders"][0]
        cu = comp["computer_uuid"]
    except Exception as e:
        out["error"] = f"reader list failed: {e}; {lst.stderr[-300:]}"
        return out
    rdest = workdir / "from_arq"
    if rdest.exists():
        shutil.rmtree(rdest)
    rdest.mkdir(parents=True)
    restore_args = ["arq_reader.cli", "restore", str(arq_dest), folder,
                    str(rdest), "--password-env", PW_ENV, "--verify-after"]
    if computer_uuid:
        restore_args += ["--computer-uuid", cu]
    r = _py(*restore_args, pw_env=arq_pw)
    out["reader_exit"] = r.returncode
    try:
        rj = json.loads(r.stdout[r.stdout.index("{"):])
        out["restore_failures"] = rj.get("failures", [])
        out["verify"] = rj.get("verify", {})
    except Exception:
        out["verify"] = {"note": "summary parse failed"}
    out["scenarios"] = diff_all(fixtures, rdest)
    return out


# --- Format drift (fingerprint of Arq's emit vs baseline) ------------------

def baseline_and_drift(arq_dest: Path, arq_pw: str, version: str,
                       computer_uuid: Optional[str] = None) -> Dict:
    BASELINES.mkdir(parents=True, exist_ok=True)
    cur = BASELINES / f"{version}.fp.json"
    fp_args = ["arq_validator.fingerprint_cli", "compute", str(arq_dest),
               "--password-env", PW_ENV, "--max-records-per-folder", "1",
               "--out", str(cur)]
    # Restrict to the round-trip plan's folder when the destination is shared
    # (avoids fingerprinting — or needing the password for — other plans).
    if computer_uuid:
        fp_args += ["--computer-uuid", computer_uuid]
    fp = _py(*fp_args, pw_env=arq_pw)
    if fp.returncode != 0:
        return {"error": f"fingerprint failed: {fp.stderr[-300:]}"}
    out: Dict = {"baseline": str(cur.relative_to(REPO))}
    # find the most recent prior baseline (different version)
    priors = sorted(p for p in BASELINES.glob("*.fp.json") if p != cur)
    if not priors:
        out["drift"] = "no prior baseline (first version captured)"
        return out
    prev = priors[-1]
    cmp = _py("arq_validator.fingerprint_cli", "compare", str(prev), str(cur))
    out["compared_against"] = prev.stem
    try:
        cdata = json.loads(cmp.stdout)
        out["match"] = cdata.get("match")
        out["sidecar_schema_diffs"] = cdata.get("summary", {}).get(
            "sidecar_schema_diffs", "?")
        out["drift"] = ("none — schema matches prior version"
                        if cdata.get("match") else "DRIFT — see run report")
        out["drift_detail"] = cdata.get("sidecar_schema_diffs", [])
    except Exception as e:
        out["drift"] = f"compare parse error: {e}"
    return out


# --- reporting / accumulation ----------------------------------------------

def _status_roll(scn_map: Dict[str, Dict[str, str]]) -> str:
    if not scn_map:
        return "—"
    sts = [v["status"] for v in scn_map.values()]
    if any(s == "FAIL" for s in sts):
        return f"FAIL ({sum(s=='FAIL' for s in sts)}/{len(sts)})"
    if any(s == "PASS_NORM" for s in sts):
        return "PASS*"
    return "PASS"


def _scrub_paths(text: str) -> str:
    """Strip machine-specific absolute paths from report text so committed
    reports never leak personal filesystem layout. The repo prefix becomes a
    repo-relative path; any remaining home-dir path becomes ``~/…``. (The
    reader's verify-failure JSON, for one, embeds the absolute restore-target
    path — this keeps it out of the accumulated docs.)"""
    repo = str(REPO)
    text = text.replace(repo + os.sep, "").replace(repo, ".")
    home = str(Path.home())
    text = text.replace(home + os.sep, "~" + os.sep).replace(home, "~")
    return text


def existing_report(version: str) -> Optional[Path]:
    """First accumulated run report for this Arq version, if any.

    A version is considered "already tested" once any
    ``runs/<version>_<date>.md`` exists. Used by ``all`` to no-op on a
    version that already has a report, so the suite can be run on a periodic
    schedule and only does work when a new Arq.app version appears.
    """
    if not RUNS.exists():
        return None
    hits = sorted(RUNS.glob(f"{version}_*.md"))
    return hits[0] if hits else None


def server_db_schema_drift(version: str) -> Dict:
    """Capture Arq's local server.db schema fingerprint for this version and
    diff it against the previous version's — a sentinel for the unversioned,
    silently-changing local config/activity DB the read-only mirror depends
    on. Stored as baselines/server-db-schema-<version>.json."""
    fp = arq_db_schema_fingerprint()
    if not any(fp.values()):
        return {"server_db_schema": "unavailable (server.db not readable)"}
    BASELINES.mkdir(parents=True, exist_ok=True)
    cur = BASELINES / f"server-db-schema-{version}.json"
    cur.write_text(json.dumps(fp, indent=1, sort_keys=True))
    out: Dict = {"baseline": str(cur.relative_to(REPO))}
    priors = sorted(p for p in BASELINES.glob("server-db-schema-*.json")
                    if p != cur)
    if not priors:
        out["server_db_schema"] = "no prior baseline (first version captured)"
        return out
    prev = json.loads(priors[-1].read_text())
    diffs = {}
    for tbl in set(fp) | set(prev):
        added = sorted(set(fp.get(tbl, [])) - set(prev.get(tbl, [])))
        removed = sorted(set(prev.get(tbl, [])) - set(fp.get(tbl, [])))
        if added or removed:
            diffs[tbl] = {"added": added, "removed": removed}
    out["compared_against"] = priors[-1].stem
    out["server_db_schema"] = ("none — local DB schema unchanged"
                               if not diffs else "DRIFT — see detail")
    if diffs:
        out["server_db_schema_detail"] = diffs
    return out


def _run_problems(res: Dict) -> List[str]:
    """Alarm-worthy findings for `--notify` (content FAILs, Arq-side errors,
    local-DB schema drift). Benign plan-config fingerprint polymorphism
    (baseline_and_drift `match:false`) is intentionally NOT alarmed."""
    probs: List[str] = []
    for cfg, c in res.get("direction_a", {}).get("configs", {}).items():
        f = [s for s, r in c.get("reader_scenarios", {}).items()
             if r["status"] == "FAIL"]
        if f:
            probs.append(f"Dir-A {cfg} FAIL {f[:3]}")
    db = res.get("direction_b")
    if isinstance(db, dict):
        if db.get("error"):
            probs.append(f"Dir-B error: {str(db['error'])[:80]}")
        else:
            f = [s for s, r in db.get("scenarios", {}).items()
                 if r["status"] == "FAIL"]
            if f:
                probs.append(f"Dir-B FAIL {f[:3]}")
            if db.get("arq_error_count"):
                probs.append(f"Arq errorCount={db['arq_error_count']}")
    sdb = res.get("server_db_drift", {}).get("server_db_schema", "")
    if isinstance(sdb, str) and sdb.startswith("DRIFT"):
        probs.append("server.db schema DRIFT")
    return probs


def write_report(version: str, result: Dict) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    date = _dt.date.today().isoformat()
    path = RUNS / f"{version}_{date}.md"
    L: List[str] = []
    L.append(f"# Arq {version} compatibility run — {date}\n")
    L.append(f"- Arq.app version: **{version}**")
    L.append(f"- Host: {sys.platform}, Python {sys.version.split()[0]}")
    L.append(f"- Generated by `scripts/arq_compat/run.py`\n")

    da = result.get("direction_a")
    if da:
        L.append("## Direction A — our writer → Arq (read back)\n")
        for cfg, c in da.get("configs", {}).items():
            roll = _status_roll(c.get("reader_scenarios", {}))
            L.append(f"### config `{cfg}` — content round-trip: **{roll}**")
            v = c.get("verify", {})
            vfails = v.get("failures", []) if isinstance(v, dict) else []
            L.append(f"- writer_ok={c.get('writer_ok')} "
                     f"reader_exit={c.get('reader_exit')} "
                     f"restore_failures={len(c.get('restore_failures', []))} "
                     f"verify_ok={v.get('ok') if isinstance(v, dict) else '?'}")
            if vfails:
                L.append(f"- verify notes (not content-diff failures): "
                         f"`{json.dumps(vfails)[:300]}`")
            L.append("\n| scenario | status | detail |")
            L.append("|---|---|---|")
            for s, r in c.get("reader_scenarios", {}).items():
                L.append(f"| {s} | {r['status']} | {r['detail']} |")
            L.append("")
        L.append("**Arq.app GUI restore (manual leg):** add a Direction-A "
                 "destination above as a storage location in Arq.app, restore "
                 "it, then run `run.py confirm-gui-restore --restored <dir>`.\n")

    db = result.get("direction_b")
    if db:
        roll = _status_roll(db.get("scenarios", {}))
        L.append("## Direction B — Arq → our reader\n")
        if db.get("error"):
            L.append(f"- not run / error: `{db['error']}`\n")
        else:
            v = db.get("verify", {})
            vfails = v.get("failures", []) if isinstance(v, dict) else []
            L.append(f"### content round-trip: **{roll}**")
            L.append(f"- backup={db.get('completed')} "
                     f"computer_uuid={db.get('computer_uuid')} "
                     f"reader_exit={db.get('reader_exit')} "
                     f"restore_failures={len(db.get('restore_failures', []))} "
                     f"verify_ok={v.get('ok') if isinstance(v, dict) else '?'}")
            if "arq_error_count" in db:
                L.append(f"- Arq-side (latestBackupActivityJSON): "
                         f"errorCount={db.get('arq_error_count')} "
                         f"maxErrorSeverity={db.get('arq_max_error_severity')} "
                         f"aborted={db.get('arq_aborted')}")
            if db.get("arq_activity"):
                L.append(f"- Arq `activities` row (ground truth): "
                         f"`{json.dumps(db['arq_activity'])}`")
            if vfails:
                L.append(f"- verify notes (not content-diff failures): "
                         f"`{json.dumps(vfails)[:300]}`")
            L.append("\n| scenario | status | detail |")
            L.append("|---|---|---|")
            for s, r in db.get("scenarios", {}).items():
                L.append(f"| {s} | {r['status']} | {r['detail']} |")
            L.append("")

    dr = result.get("drift")
    if dr:
        L.append("## Format drift (Arq emit vs previous version)\n")
        for k, v in dr.items():
            if k == "drift_detail":
                continue
            L.append(f"- {k}: {v}")
        if dr.get("drift_detail"):
            L.append("\n```json")
            L.append(json.dumps(dr["drift_detail"], indent=1)[:2000])
            L.append("```")
        L.append("")

    sdb = result.get("server_db_drift")
    if sdb:
        L.append("## Local server.db schema drift (vs previous version)\n")
        for k, v in sdb.items():
            if k == "server_db_schema_detail":
                continue
            L.append(f"- {k}: {v}")
        if sdb.get("server_db_schema_detail"):
            L.append("\n```json")
            L.append(json.dumps(sdb["server_db_schema_detail"], indent=1)[:2000])
            L.append("```")
        L.append("")

    path.write_text(_scrub_paths("\n".join(L)))
    return path


def update_matrix(version: str, result: Dict, report: Path) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    mx = DOCS / "MATRIX.md"
    date = _dt.date.today().isoformat()
    da = result.get("direction_a", {})
    a_roll = "—"
    if da.get("configs"):
        rolls = [_status_roll(c.get("reader_scenarios", {}))
                 for c in da["configs"].values()]
        a_roll = "FAIL" if any("FAIL" in r for r in rolls) else (
            "PASS*" if any("*" in r for r in rolls) else "PASS")
    db = result.get("direction_b", {})
    b_roll = "—" if not db else (
        db.get("error") and "not run" or _status_roll(db.get("scenarios", {})))
    drift = result.get("drift", {}).get("drift", "—")
    rel = report.relative_to(DOCS)
    row = (f"| {date} | {version} | {a_roll} | {b_roll} | {drift} "
           f"| [report]({rel}) |")
    header = (
        "# Arq compatibility matrix (accumulating)\n\n"
        "Each row is one run of `scripts/arq_compat/run.py` against an "
        "installed Arq.app version. Re-run after every Arq update. `PASS*` "
        "= content byte-identical but a filename came back NFC/NFD-normalised "
        "(Arq restore-side behaviour, not a data gap). Per-scenario detail is "
        "in the linked report.\n\n"
        "| Date | Arq version | Dir A (writer→Arq) | Dir B (Arq→reader) "
        "| Format drift | Report |\n"
        "|---|---|---|---|---|---|\n"
    )
    if mx.exists():
        txt = mx.read_text()
        if "|---|---|---|---|---|---|" in txt:
            mx.write_text(txt.rstrip() + "\n" + row + "\n")
            return
    mx.write_text(header + row + "\n")


# --- CLI -------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        # Default under the project directory (not /tmp) so the Arq.app GUI
        # folder picker can reach <workdir>/fixtures for the Direction-B plan
        # source. Run artifacts here are git-ignored.
        p.add_argument("--workdir", type=Path,
                       default=REPO / "arq_compat_run")

    pa = sub.add_parser("direction-a", help="writer->reader (auto) + GUI prep")
    add_common(pa)
    pa.add_argument("--arq-restore-bin", default=None)

    # The Arq destination's encryption password is read from a file
    # (default .secrets/dest_password) — never passed inline so it can't
    # appear in this process's `ps` argv.
    default_pw_file = REPO / ".secrets" / "dest_password"

    pb = sub.add_parser("direction-b", help="Arq backup -> our reader")
    add_common(pb)
    pb.add_argument("--plan-uuid", required=True)
    pb.add_argument("--arq-dest", required=True, type=Path)
    pb.add_argument("--arq-pw-file", type=Path, default=default_pw_file)
    pb.add_argument("--computer-uuid", default=None,
                    help="target one plan's folder in a shared destination")
    pb.add_argument("--skip-backup", action="store_true",
                    help="don't trigger arqc; operator already backed up")

    pc = sub.add_parser("confirm-gui-restore", help="diff an Arq GUI restore")
    add_common(pc)
    pc.add_argument("--restored", required=True, type=Path)
    pc.add_argument("--plan-uuid", default=None,
                    help="attach Arq's own restore-activity evidence")

    pbl = sub.add_parser("baseline", help="fingerprint Arq emit + drift")
    pbl.add_argument("--arq-dest", required=True, type=Path)
    pbl.add_argument("--arq-pw-file", type=Path, default=default_pw_file)
    pbl.add_argument("--computer-uuid", default=None)

    pall = sub.add_parser("all", help="run automatable legs + report")
    add_common(pall)
    pall.add_argument("--arq-restore-bin", default=None)
    pall.add_argument("--plan-uuid", default=None)
    pall.add_argument("--arq-dest", default=None, type=Path)
    pall.add_argument("--arq-pw-file", type=Path, default=default_pw_file)
    pall.add_argument("--computer-uuid", default=None,
                      help="target one plan's folder in a shared destination")
    pall.add_argument("--skip-backup", action="store_true",
                      help="don't trigger arqc; operator already backed up")
    pall.add_argument("--force", action="store_true",
                      help="re-run even if this Arq version already has a "
                           "report (default: skip already-tested versions)")
    pall.add_argument("--notify", action="store_true",
                      help="fire a macOS notification on any FAIL / drift "
                           "(for scheduled/unattended runs)")

    args = ap.parse_args(argv)
    ver = arq_version()

    def _read_pw(p: Optional[Path]) -> Optional[str]:
        if p and Path(p).exists():
            return Path(p).read_text().strip()
        return None

    if args.cmd == "direction-a":
        da = direction_a(args.workdir, args.arq_restore_bin)
        res = {"direction_a": da}
        rep = write_report(ver, res)
        update_matrix(ver, res, rep)
        print(f"report: {rep}")
        print("[GUI leg] throwaway password for the writer_* destinations: "
              + da["gui_pw"])
        return 0

    if args.cmd == "direction-b":
        pw = _read_pw(args.arq_pw_file)
        res = {"direction_b": direction_b(
            args.workdir, args.plan_uuid, args.arq_dest, pw,
            computer_uuid=args.computer_uuid, skip_backup=args.skip_backup)}
        print(json.dumps(res, ensure_ascii=False, indent=2)[:2000])
        return 0

    if args.cmd == "confirm-gui-restore":
        print(json.dumps(confirm_gui_restore(args.workdir, args.restored,
                                             plan_uuid=args.plan_uuid),
                         ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "baseline":
        pw = _read_pw(args.arq_pw_file)
        print(json.dumps(
            baseline_and_drift(args.arq_dest, pw, ver,
                               computer_uuid=args.computer_uuid),
            ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "all":
        # Idempotent for periodic (cron) runs: if this Arq version already
        # has a report, do nothing and exit 0 — work happens only when a new
        # Arq.app version appears. `--force` overrides.
        prior = existing_report(ver)
        if prior is not None and not args.force:
            print(f"Arq {ver}: report already exists "
                  f"({prior.relative_to(REPO)}); nothing to do. "
                  f"Use --force to re-run.")
            return 0
        if prior is not None:
            print(f"Arq {ver}: report exists but --force given — re-running.")
        else:
            print(f"Arq {ver}: no report yet — running compatibility checks.")
        res: Dict = {}
        pw = _read_pw(args.arq_pw_file)
        da = direction_a(args.workdir, args.arq_restore_bin)
        res["direction_a"] = da
        if args.plan_uuid and args.arq_dest and pw:
            res["direction_b"] = direction_b(
                args.workdir, args.plan_uuid, args.arq_dest, pw,
                computer_uuid=args.computer_uuid,
                skip_backup=args.skip_backup)
        if args.arq_dest and pw:
            res["drift"] = baseline_and_drift(
                args.arq_dest, pw, ver, computer_uuid=args.computer_uuid)
        # Local server.db schema drift always runs (server.db is readable).
        res["server_db_drift"] = server_db_schema_drift(ver)
        rep = write_report(ver, res)
        update_matrix(ver, res, rep)
        print(f"Arq {ver} — report: {rep}")
        print("[GUI leg] throwaway password for the writer_* destinations: "
              + da["gui_pw"])
        # Scheduled use: alert on any FAIL or DRIFT so an unattended run
        # surfaces a regression without watching the log.
        if args.notify:
            problems = _run_problems(res)
            if problems:
                _notify(f"Arq compat {ver}: ATTENTION",
                        "; ".join(problems)[:240])
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
