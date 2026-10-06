import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id, observed_at),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate_sources_unique(conn)
        finally:
            conn.close()

    def _migrate_sources_unique(self, conn):
        # 旧库 sources 只有 UNIQUE(item_id, source_type, external_id)，
        # 无法保存同一来源的多批观测；检测到旧定义时就地迁移。
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'"
        ).fetchone()["sql"]
        if "observed_at" in sql.split("UNIQUE", 1)[-1]:
            return
        conn.execute("ALTER TABLE sources RENAME TO sources_old")
        conn.execute(
            """
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                external_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(item_id, source_type, external_id, observed_at),
                FOREIGN KEY(item_id) REFERENCES items(id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sources(id,item_id,source_type,external_id,payload,observed_at,created_at)
            SELECT id,item_id,source_type,external_id,payload,observed_at,created_at FROM sources_old
            """
        )
        conn.execute("DROP TABLE sources_old")

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def _source_rows(self, conn, item_id):
        rows = conn.execute(
            "SELECT * FROM sources WHERE item_id=? ORDER BY id", (item_id,)
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            result.append(value)
        return result

    def record_source(self, item_id, source_type, external_id, payload, observed_at,
                      actor, role, reconciler, apply_change):
        """在单个事务内完成：取锁 -> 去重/幂等 -> 对账 -> 失效重算 -> 落库。

        reconciler(rows) 由规则层提供，返回对账结果；apply_change(item, reconciled)
        返回 (new_status, new_payload, event_payload)。
        返回 (source_result, created: bool)。
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            item = self._row_to_item(row)
            if item["status"] in {"resolved", "cancelled"}:
                raise DomainError("item_closed", "接近事件已结束，不再接收来源观测", 409)

            key_rows = conn.execute(
                "SELECT payload, observed_at FROM sources "
                "WHERE item_id=? AND source_type=? AND external_id=? ORDER BY id",
                (item_id, source_type, external_id),
            ).fetchall()
            incoming_when = observed_at
            latest_when = key_rows[-1]["observed_at"] if key_rows else None
            if latest_when is not None and incoming_when < latest_when:
                raise ConflictError(
                    "stale_observation",
                    "该来源已有更新观测 %s，晚到记录 %s 不能回退" % (latest_when, incoming_when),
                )
            if incoming_when == latest_when:
                latest_payload = json.loads(key_rows[-1]["payload"])
                if latest_payload == payload:
                    # 并发/重试的重复提交：幂等返回，结果不变。
                    conn.execute("COMMIT")
                    return {
                        "id": None,
                        "item_id": item_id,
                        "source_type": source_type,
                        "external_id": external_id,
                        "payload": payload,
                        "observed_at": observed_at,
                        "duplicated": True,
                    }, False
                raise ConflictError(
                    "source_conflict",
                    "同一来源同一观测时刻提交了不同的距离/协方差",
                )

            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                # 并发下另一个请求抢先插入同一观测，退化为幂等/冲突判定。
                clash = conn.execute(
                    "SELECT payload FROM sources WHERE item_id=? AND source_type=? AND external_id=? AND observed_at=?",
                    (item_id, source_type, external_id, observed_at),
                ).fetchone()
                if clash is not None and json.loads(clash["payload"]) == payload:
                    conn.execute("COMMIT")
                    return {
                        "id": None,
                        "item_id": item_id,
                        "source_type": source_type,
                        "external_id": external_id,
                        "payload": payload,
                        "observed_at": observed_at,
                        "duplicated": True,
                    }, False
                raise ConflictError("duplicate_source", "同一来源记录已经提交")

            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

            rows = self._source_rows(conn, item_id)
            reconciled = reconciler(rows)
            event_payload = {
                "source_id": source_id,
                "source_type": source_type,
                "external_id": external_id,
                "observed_at": observed_at,
                "reconciled": {
                    "miss_distance_m": reconciled["miss_distance_m"],
                    "covariance_m": reconciled["covariance_m"],
                    "source_count": reconciled["source_count"],
                    "fingerprint": reconciled["fingerprint"],
                },
            }

            fingerprint_before = item["payload"].get("source_fingerprint")
            if reconciled["fingerprint"] != fingerprint_before:
                new_status, new_payload, change_event = apply_change(item, reconciled)
                event_payload.update(change_event)
                version = int(row["version"]) + 1
                conn.execute(
                    "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                    (new_status, version, canonical_json(new_payload), now_iso(), item_id),
                )
                conn.execute(
                    "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, "source_recorded", actor, role,
                     canonical_json({"source_id": source_id, **change_event}), now_iso()),
                )
                self.append_audit(conn, item_id, "sources_reconciled", actor, role, event_payload)
            else:
                self.append_audit(conn, item_id, "source_recorded", actor, role, event_payload)
            conn.execute("COMMIT")
            return {
                "id": source_id,
                "item_id": item_id,
                "source_type": source_type,
                "external_id": external_id,
                "payload": payload,
                "observed_at": observed_at,
                "duplicated": False,
                "reconciled": reconciled,
            }, True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def recover_assessment(self, item_id, actor, role, reconciler, rebuild):
        """从来源记录重建并重算风险评估（单个事务）。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            item = self._row_to_item(row)
            if item["payload"].get("assessment_state") != "failed":
                raise DomainError("assessment_current", "风险评估未失败，不需要恢复", 409)
            rows = self._source_rows(conn, item_id)
            if not rows:
                raise DomainError("no_sources", "没有来源记录，无法重建评估")
            reconciled = reconciler(rows)
            new_status, new_payload, event_payload = rebuild(item, reconciled)
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "recover_assessment", actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, "assessment_recovered", actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            return self._source_rows(conn, item_id)
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
