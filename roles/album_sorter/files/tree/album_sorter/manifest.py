"""SQLite manifest: the pipeline's memory (spec Section 3).

Every phase is a function of manifest state, which is what makes
runs idempotent and resumable. Schema is created step by step
(this runtime's sqlite is not transactional for DDL - see
AGENTS.md - so creation is idempotent CREATE IF NOT EXISTS only).
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS items (
        md5 TEXT PRIMARY KEY,
        done_name TEXT NOT NULL,
        clean_name TEXT,
        suffix_verified INTEGER NOT NULL DEFAULT 0,
        capture_time_raw TEXT,
        capture_time TEXT,
        time_source TEXT,
        time_confidence TEXT,
        device TEXT,
        software TEXT,
        gps_lat REAL,
        gps_lon REAL,
        gps_utc TEXT,
        file_size INTEGER,
        file_mtime REAL,
        category TEXT,
        category_confidence REAL,
        category_source TEXT,
        analysis_json TEXT,
        embedding BLOB,
        embedding_model TEXT,
        episode_id INTEGER,
        assign_confidence REAL,
        assign_method TEXT,
        status TEXT NOT NULL DEFAULT 'ingested',
        final_path TEXT,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS episodes (
        episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT,
        span_start TEXT,
        span_end TEXT,
        item_count INTEGER NOT NULL DEFAULT 0,
        exemplars TEXT,
        outcome TEXT NOT NULL DEFAULT 'pending',
        album_name TEXT,
        gate_character TEXT,
        notability_reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        started TEXT,
        finished TEXT,
        phase TEXT,
        params TEXT,
        notes TEXT
    )
    """,
    # Decision journal + stage checkpoints (backlog B32):
    # every paid model verdict, keyed by a hash of the exact
    # question (see journal.py), and per-stage completion
    # fingerprints for the one monolithic stage (grouping).
    # Additive-only, like everything in this schema: old code
    # opening a new database ignores both tables.
    """
    CREATE TABLE IF NOT EXISTS decisions (
        decision_key TEXT PRIMARY KEY,
        stage TEXT NOT NULL,
        subject TEXT,
        result_json TEXT NOT NULL,
        model TEXT,
        prompt_hash TEXT,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        created TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_checkpoints (
        stage TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        stats TEXT,
        completed TEXT
    )
    """,
    # B41: manifest-level key/value facts that are not stage
    # state (first occupant: the phase-1 schema version the
    # batch started under - the batch-boundary rule needs to
    # know whether a manifest's analyses predate the current
    # schema even after every item has left 'ingested').
    """
    CREATE TABLE IF NOT EXISTS manifest_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # B33: per-call usage ledger - one row per completed API
    # call, written at call time so a crashed run still
    # leaves a complete spend account (see usage.py).
    """
    CREATE TABLE IF NOT EXISTS api_calls (
        id INTEGER PRIMARY KEY,
        ts TEXT,
        stage TEXT,
        subject TEXT,
        model TEXT,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        service_tier TEXT
    )
    """,
    # B35 (no-re-spend audit): the raw-response spill - one
    # row per obtained response, written by the recording
    # client before the caller sees the result, so output
    # that fails parsing or whose stage dies mid-apply is
    # a re-parseable asset instead of a re-buy.
    """
    CREATE TABLE IF NOT EXISTS raw_responses (
        id INTEGER PRIMARY KEY,
        ts TEXT,
        stage TEXT,
        subject TEXT,
        model TEXT,
        raw_text TEXT,
        decision_key TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisions_stage ON decisions(stage)",
    "CREATE INDEX IF NOT EXISTS idx_items_status ON items(status)",
    "CREATE INDEX IF NOT EXISTS idx_items_done_name ON items(done_name)",
    "CREATE INDEX IF NOT EXISTS idx_items_capture_time ON items(capture_time)",
]


class Manifest:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = path
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        # Autocommit mode: the sqlite3 module never opens an
        # implicit transaction, so execute() controls commit
        # per statement (the historical behavior) and
        # transaction() can bracket a real unit of work with
        # explicit BEGIN/COMMIT (B32). Journal mode stays the
        # default - backup() copies the DB file, which would
        # silently omit WAL contents.
        self.db.isolation_level = None
        self._txn_depth = 0
        # F-1 fix (B35 review): the usage/spill sinks write
        # through a DEDICATED connection (see _ledger_conn).
        # Phase 1 calls the client from worker threads, and
        # the main connection is thread-bound - every sink
        # write from a worker raised ProgrammingError and
        # was swallowed by the sink guard, silently losing
        # 100% of Phase 1's manifest usage rows and spills.
        self._ledger_db: sqlite3.Connection | None = None
        self._ledger_lock = threading.Lock()
        # a reader (report/stats) during a writer's brief
        # transaction should wait, not die with 'database is locked'
        self.db.execute("PRAGMA busy_timeout = 5000")
        for stmt in SCHEMA:
            self.db.execute(stmt)
        self.db.commit()
        # additive migrations for older databases (single ALTERs,
        # no rebuild - DDL here is not transactional, keep it trivial)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(episodes)")}
        if "notability_reason" not in cols:
            self.db.execute("ALTER TABLE episodes ADD COLUMN notability_reason TEXT")
        if "name_basis" not in cols:
            self.db.execute("ALTER TABLE episodes ADD COLUMN name_basis TEXT")
            self.db.commit()
        icols = {r[1] for r in self.db.execute("PRAGMA table_info(items)")}
        if "file_size" not in icols:
            self.db.execute("ALTER TABLE items ADD COLUMN file_size INTEGER")
            self.db.commit()
        if "file_mtime" not in icols:
            self.db.execute("ALTER TABLE items ADD COLUMN file_mtime REAL")
            self.db.commit()
        if "software" not in icols:
            self.db.execute("ALTER TABLE items ADD COLUMN software TEXT")
            self.db.commit()
        if "gps_utc" not in icols:
            self.db.execute("ALTER TABLE items ADD COLUMN gps_utc TEXT")
            self.db.commit()
        if "phase1_attempts" not in icols:
            self.db.execute("ALTER TABLE items ADD COLUMN"
                            " phase1_attempts INTEGER NOT NULL DEFAULT 0")
            self.db.commit()
        # B35: the full usage picture on both accounting
        # stores - thoughts tokens (billed as output, never
        # recorded before B35) and the call outcome.
        rcols = {r[1] for r in self.db.execute("PRAGMA table_info(raw_responses)")}
        if rcols and "decision_key" not in rcols:
            self.db.execute(
                "ALTER TABLE raw_responses ADD COLUMN decision_key TEXT")
            self.db.commit()
        acols = {r[1] for r in self.db.execute("PRAGMA table_info(api_calls)")}
        for name, decl in (("thoughts", "INTEGER NOT NULL DEFAULT 0"),
                           ("cached", "INTEGER NOT NULL DEFAULT 0"),
                           ("total_tokens", "INTEGER NOT NULL DEFAULT 0"),
                           ("outcome", "TEXT NOT NULL DEFAULT 'ok'"),
                           ("service_tier", "TEXT")):
            if name not in acols:
                self.db.execute(
                    f"ALTER TABLE api_calls ADD COLUMN {name} {decl}")
                self.db.commit()
        dcols = {r[1] for r in self.db.execute("PRAGMA table_info(decisions)")}
        if "tokens_thoughts" not in dcols:
            self.db.execute(
                "ALTER TABLE decisions ADD COLUMN tokens_thoughts"
                " INTEGER NOT NULL DEFAULT 0")
            self.db.commit()
        if "context_json" not in dcols:
            # B35 v2 keys: the judged volatile context (span,
            # distance, counts) a verdict was rendered under,
            # stored beside it so replay can re-validate the
            # drift deterministically instead of baking the
            # volatile fields into the key (where any growth
            # orphaned the verdict).
            self.db.execute(
                "ALTER TABLE decisions ADD COLUMN context_json TEXT")
            self.db.commit()

    # ---- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.db.execute(sql, params)
        if self._txn_depth == 0:
            self.db.commit()
        return cur

    @contextlib.contextmanager
    def transaction(self):
        """A real unit of work over the manifest (B32).

        Historically every statement committed on its own, so
        a multi-write apply (a merge, a linkage attach plus
        its span recompute, grouping's final pass) could be
        left half-applied by a crash. Inside this scope,
        statements accumulate and commit - or roll back -
        together. Nested scopes run under a SAVEPOINT: an
        exception escaping an inner scope rolls back only
        that scope's writes, and the outer scope decides
        whether the whole unit still stands. Writers stay on
        the main thread under the run lock, as everywhere
        else here."""
        outer = self._txn_depth == 0
        savepoint = None
        if outer:
            self.db.execute("BEGIN IMMEDIATE")
        else:
            savepoint = f"b32_sp_{self._txn_depth}"
            self.db.execute(f"SAVEPOINT {savepoint}")
        self._txn_depth += 1
        try:
            yield
        except BaseException:
            self._txn_depth -= 1
            if outer:
                self.db.execute("ROLLBACK")
            else:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        self._txn_depth -= 1
        try:
            if outer:
                self.db.execute("COMMIT")
            else:
                self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        except BaseException:
            # COMMIT/RELEASE itself failed (disk full is the
            # plausible cause): the connection's transaction
            # state is uncertain, so roll back whatever
            # remains and resync the depth counter before
            # propagating, rather than leaving later execute()
            # calls running inside a lingering transaction.
            try:
                self.db.execute("ROLLBACK")
            except Exception:
                pass
            self._txn_depth = 0
            raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.db.execute(sql, params))

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- items ------------------------------------------------------------
    def get_by_done_name(self, name: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM items WHERE done_name = ?", (name,))

    def get_by_md5(self, md5: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM items WHERE md5 = ?", (md5,))

    def upsert_item(self, **fields: Any) -> None:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.execute(
            f"INSERT INTO items ({cols}) VALUES ({marks}) "
            "ON CONFLICT(md5) DO UPDATE SET " +
            ", ".join(f"{c} = excluded.{c}" for c in fields if c != "md5"),
            tuple(fields.values()),
        )

    def update_item(self, md5: str, **fields: Any) -> None:
        sets = ", ".join(f"{c} = ?" for c in fields)
        self.execute(f"UPDATE items SET {sets} WHERE md5 = ?",
                     tuple(fields.values()) + (md5,))

    def items_by_status(self, status: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM items WHERE status = ? ORDER BY capture_time", (status,))

    # ---- episodes -----------------------------------------------------------
    def create_episode(self, description: str, exemplars: Iterable[str]) -> int:
        cur = self.execute(
            "INSERT INTO episodes (description, exemplars) VALUES (?, ?)",
            (description, json.dumps(list(exemplars))),
        )
        return int(cur.lastrowid)

    def update_episode(self, episode_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{c} = ?" for c in fields)
        self.execute(f"UPDATE episodes SET {sets} WHERE episode_id = ?",
                     tuple(fields.values()) + (episode_id,))

    def get_episode(self, episode_id: int) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,))

    def all_episodes(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM episodes ORDER BY span_start")

    def recompute_episode_span(self, episode_id: int) -> None:
        row = self.query_one(
            "SELECT MIN(capture_time) AS s, MAX(capture_time) AS e, COUNT(*) AS n "
            "FROM items WHERE episode_id = ?", (episode_id,))
        if row:
            self.update_episode(episode_id, span_start=row["s"], span_end=row["e"],
                                item_count=row["n"])

    # ---- safety ---------------------------------------------------------------
    def backup(self, tag: str, mirror_dir: Path | None = None) -> Path:
        """Copy the manifest file next to itself. Placement history
        lives only here; files on disk survive any manifest loss,
        but the mapping would not - so back up before the phases
        that move files or rewrite state.

        The copy is written to a temp name, verified
        (integrity_check on the copy itself), and renamed into
        place (B35 sweep): a crash mid-copy used to leave a torn
        file wearing a valid backup name, and a same-second
        same-tag backup silently overwrote the previous one. A
        JSON sidecar records the snapshot's journal size - the
        number a restore would roll back to (a stale restore
        silently forfeits every verdict bought after it; the
        sidecar makes that delta visible before anyone copies
        a backup back).

        If mirror_dir is given, the verified snapshot is also
        copied there (a different volume in production - the
        pixel share), so the mapping survives loss of the work
        volume too. A failed mirror raises: callers take the
        backup before moving any files, so placement aborts
        instead of proceeding with only a same-volume snapshot."""
        import os
        import shutil
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        dst = self.db_path.with_suffix(f".bak-{stamp}-{tag}")
        n = 2
        while dst.exists():
            dst = self.db_path.with_suffix(f".bak-{stamp}-{tag}-{n}")
            n += 1
        if self._txn_depth == 0:
            self.db.commit()
        tmp = dst.with_name(dst.name + f".tmp{os.getpid()}")
        try:
            shutil.copy2(self.db_path, tmp)
            check = sqlite3.connect(str(tmp))
            try:
                ok = check.execute("PRAGMA integrity_check").fetchone()[0]
                if ok != "ok":
                    raise RuntimeError(f"backup copy failed integrity: {ok}")
                decisions = check.execute(
                    "SELECT COUNT(*) FROM decisions").fetchone()[0]
                items = check.execute(
                    "SELECT COUNT(*) FROM items").fetchone()[0]
            finally:
                check.close()
            os.replace(tmp, dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        Path(str(dst) + ".json").write_text(json.dumps({
            "tag": tag, "created": stamp, "source": str(self.db_path),
            "decisions": decisions, "items": items}))
        if mirror_dir is not None:
            mirror_dir.mkdir(parents=True, exist_ok=True)
            mtmp = mirror_dir / (dst.name + f".tmp{os.getpid()}")
            try:
                shutil.copy2(dst, mtmp)
                os.replace(mtmp, mirror_dir / dst.name)
            except BaseException:
                mtmp.unlink(missing_ok=True)
                raise
            shutil.copy2(Path(str(dst) + ".json"),
                         mirror_dir / (dst.name + ".json"))
        return dst

    # ---- decision journal + checkpoints (B32) ------------------------------
    def get_decision(self, decision_key: str) -> Optional[sqlite3.Row]:
        return self.query_one(
            "SELECT * FROM decisions WHERE decision_key = ?", (decision_key,))

    def record_decision(self, decision_key: str, stage: str, subject: str,
                        result: dict, model: str = "", prompt_hash: str = "",
                        tokens_in: int = 0, tokens_out: int = 0,
                        replace: bool = False,
                        tokens_thoughts: int = 0,
                        context: dict | None = None) -> None:
        """Store one paid model verdict. Callers invoke this
        inside the same transaction as the state change the
        verdict drives, so the journal and the manifest can
        never diverge.

        Default is INSERT OR IGNORE: stages consult the
        journal before calling, so an existing row means
        this exact question was already answered and the
        first verdict stands. Gates used replace=True until
        B35 - they re-bought a fresh verdict on every run
        and could lose it entirely to a crash between the
        call and the commit; they now consult the journal
        first like every other stage (their skip mechanism
        remains the episode outcome for the normal path)."""
        from datetime import datetime, timezone
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        self.execute(
            f"{verb} INTO decisions (decision_key, stage, subject, "
            "result_json, model, prompt_hash, tokens_in, tokens_out, created,"
            " tokens_thoughts, context_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (decision_key, stage, subject, json.dumps(result), model,
             prompt_hash, tokens_in, tokens_out,
             datetime.now(timezone.utc).isoformat(), tokens_thoughts,
             json.dumps(context) if context is not None else None))

    def update_decision_result(self, decision_key: str,
                               result: dict) -> None:
        """Rewrite a journal row's stored result in place
        (B35, gates): the verdict is recorded the moment it
        is bought; outcome-application details discovered
        later (name verification, the outcome itself) are
        folded into the same row as provenance - no second
        verdict is ever purchased to update the record."""
        self.execute(
            "UPDATE decisions SET result_json = ? WHERE decision_key = ?",
            (json.dumps(result), decision_key))

    def decision_counts(self) -> dict[str, int]:
        return {r["stage"]: r["n"] for r in self.query(
            "SELECT stage, COUNT(*) AS n FROM decisions GROUP BY stage")}

    # ---- usage ledger (B33) ------------------------------------------------
    def _ledger_conn(self) -> sqlite3.Connection:
        """The dedicated connection for accounting writes
        (api_calls, raw_responses). Separate from the main
        connection for two reasons: it is usable from any
        thread (Phase 1's workers record their own calls),
        and it autocommits independently, so an accounting
        row can never be rolled back by - or trapped inside
        - a state transaction on the main connection. The
        lock serializes writers on this connection; the
        connect timeout makes a writer wait out a brief
        lock held by the main connection instead of failing.
        Only ever called after __init__ has created the
        tables on the main connection."""
        if self._ledger_db is None:
            conn = sqlite3.connect(str(self.db_path),
                                   check_same_thread=False, timeout=30)
            conn.isolation_level = None     # autocommit per row
            self._ledger_db = conn
        return self._ledger_db

    def record_api_call(self, ts: str, stage: str | None, subject: str | None,
                        model: str, tokens_in: int, tokens_out: int,
                        thoughts: int = 0, cached: int = 0,
                        total_tokens: int = 0,
                        outcome: str = "ok",
                        service_tier: str | None = None) -> None:
        """One API call, recorded at call time by the
        usage-recording client (usage.py) - including
        billed-but-failed calls (outcome != 'ok', B35). A
        plain per-statement commit is correct here: the
        row must survive the process dying on the very
        next call."""
        with self._ledger_lock:
            self._ledger_conn().execute(
                "INSERT INTO api_calls (ts, stage, subject, model, tokens_in,"
                " tokens_out, thoughts, cached, total_tokens, outcome,"
                " service_tier)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, stage, subject, model, tokens_in, tokens_out,
                 thoughts, cached, total_tokens, outcome, service_tier))

    def api_call_rows(self) -> list:
        return self.query("SELECT * FROM api_calls ORDER BY id")

    # ---- raw-response spill (B35) -------------------------------------------
    def record_raw_response(self, ts: str, stage: str | None,
                            subject: str | None, model: str,
                            raw_text: str,
                            decision_key: str | None = None) -> None:
        """One obtained response, spilled at receipt by the
        recording client (usage.py). Same per-statement
        commit rationale as record_api_call: the row must
        survive the process dying on the very next line.
        decision_key stamps the spill with the journal key
        of the question it answers (B35 sweep) - without it
        a spill cannot be matched back to its question."""
        with self._ledger_lock:
            self._ledger_conn().execute(
                "INSERT INTO raw_responses (ts, stage, subject, model,"
                " raw_text, decision_key)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, stage, subject, model, raw_text, decision_key))

    def raw_response_rows(self) -> list:
        return self.query("SELECT * FROM raw_responses ORDER BY id")

    def purge_decisions(self, stage: str | None = None) -> int:
        """The operator's re-judge path: forget stored
        verdicts (one stage, or all) so the next run asks
        those questions again. Returns rows removed."""
        if stage:
            cur = self.execute("DELETE FROM decisions WHERE stage = ?", (stage,))
        else:
            cur = self.execute("DELETE FROM decisions")
        return cur.rowcount

    def get_checkpoint(self, stage: str) -> Optional[sqlite3.Row]:
        return self.query_one(
            "SELECT * FROM stage_checkpoints WHERE stage = ?", (stage,))

    def set_checkpoint(self, stage: str, fingerprint: str, stats: dict) -> None:
        from datetime import datetime, timezone
        self.execute(
            "INSERT INTO stage_checkpoints (stage, fingerprint, stats, completed) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(stage) DO UPDATE SET "
            "fingerprint = excluded.fingerprint, stats = excluded.stats, "
            "completed = excluded.completed",
            (stage, fingerprint, json.dumps(stats),
             datetime.now(timezone.utc).isoformat()))

    def delete_checkpoint(self, stage: str) -> None:
        self.execute("DELETE FROM stage_checkpoints WHERE stage = ?", (stage,))

    # ---- manifest meta (B41) ------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        row = self.query_one(
            "SELECT value FROM manifest_meta WHERE key = ?", (key,))
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO manifest_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))

    # ---- runs ---------------------------------------------------------------
    def start_run(self, phase: str, params: dict) -> int:
        from datetime import datetime, timezone
        cur = self.execute(
            "INSERT INTO runs (started, phase, params) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), phase, json.dumps(params)),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, notes: str = "") -> None:
        from datetime import datetime, timezone
        self.execute(
            "UPDATE runs SET finished = ?, notes = ? WHERE run_id = ?",
            (datetime.now(timezone.utc).isoformat(), notes, run_id))
