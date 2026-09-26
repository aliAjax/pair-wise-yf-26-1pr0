"""到期处置台数据层：处置批次、处置项、保全冻结的存储与事务。

仅负责持久化，不含业务判断；规则来自 disposition_rules。审计写入项目共用的
audit_log 表，处置后档案、版本、副本内容均不删除，仍可通过既有接口查看。
"""
from __future__ import annotations

import sqlite3
from datetime import date

import disposition_rules as rules
from app import BusinessError, now


class DispositionRepository:
    def __init__(self, store):
        self.store = store  # 复用 PreservationStore 的连接、用户校验与 schema 迁移

    # ------------------------------------------------------------------ 内部工具
    def _user(self, conn, user_id: str, roles: set[str] | None = None):
        return self.store._user(conn, user_id, roles)

    def _audit(self, conn, archive_id: int, actor: str, action: str, detail: dict) -> None:
        self.store._audit(conn, archive_id, actor, action, detail)

    @staticmethod
    def _facts(conn, row: sqlite3.Row) -> rules.Facts:
        freeze = conn.execute(
            "SELECT reason, created_by FROM freezes WHERE item_id=? AND active=1", (row["id"],)
        ).fetchone()
        return rules.Facts(
            status=row["status"],
            retention_snapshot=row["retention_snapshot"],
            current_retention=row["retention_until"],
            frozen=freeze is not None,
            freeze_reason=freeze["reason"] if freeze else None,
            frozen_by=freeze["created_by"] if freeze else None,
        )

    _ITEM_SELECT = """
        SELECT i.id AS id, i.batch_id AS batch_id, i.archive_id AS archive_id,
               a.name AS archive_name, a.owner_id AS owner_id,
               a.retention_until AS retention_until, a.state AS archive_state,
               i.status AS status, i.retention_snapshot AS retention_snapshot,
               i.blocker_code AS blocker_code, i.blocker_message AS blocker_message,
               i.last_checked_at AS last_checked_at, i.last_checked_by AS last_checked_by,
               i.disposed_at AS disposed_at,
               (SELECT COUNT(*) FROM archive_versions v WHERE v.archive_id=i.archive_id) AS version_count,
               (SELECT COUNT(*) FROM copies c JOIN archive_versions v ON c.version_id=v.id
                  WHERE v.archive_id=i.archive_id) AS copy_count
        FROM disposition_items i JOIN archives a ON a.id=i.archive_id"""

    def _item_json(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["blocker"] = (
            {"code": d.pop("blocker_code"), "message": d.pop("blocker_message")}
            if d["blocker_code"]
            else None
        )
        return d

    def _batch_json(self, conn, batch: sqlite3.Row, today: date) -> dict:
        rows = conn.execute(
            self._ITEM_SELECT + " WHERE i.batch_id=? ORDER BY i.id", (batch["id"],)
        ).fetchall()
        items = [self._item_json(r) for r in rows]
        groups = rules.group_items(items)
        blockers = [
            {"item_id": it["id"], "archive_id": it["archive_id"], "archive_name": it["archive_name"],
             "blocker": it["blocker"]}
            for it in items
            if it["blocker"] is not None
        ]
        result = dict(batch)
        result.update({
            "groups": {k: groups[k] for k in (rules.PENDING, rules.READY, rules.FROZEN, rules.DISPOSED)},
            "blockers": blockers,
            "counts": {k: len(groups[k]) for k in (rules.PENDING, rules.READY, rules.FROZEN, rules.DISPOSED)},
        })
        return result

    # ------------------------------------------------------------------ 到期清单 / 批次
    def list_due(self, user_id: str, today: date) -> list[dict]:
        """到期档案清单：已到期且尚未在未完结批次中的档案。"""
        with self.store.connect() as conn:
            self._user(conn, user_id, {"owner", "archivist", "auditor"})
            rows = conn.execute(
                """SELECT a.id, a.name, a.owner_id, a.retention_until, a.state,
                          (SELECT COUNT(*) FROM archive_versions v WHERE v.archive_id=a.id) AS version_count,
                          (SELECT COUNT(*) FROM copies c JOIN archive_versions v ON c.version_id=v.id
                             WHERE v.archive_id=a.id) AS copy_count
                   FROM archives a
                   WHERE a.state='active' AND a.retention_until < ?
                     AND NOT EXISTS (
                         SELECT 1 FROM disposition_items i
                         WHERE i.archive_id=a.id AND i.status IN ('pending','ready','frozen'))
                   ORDER BY a.retention_until, a.id""",
                (today.isoformat(),),
            ).fetchall()
            result = [dict(r) for r in rows]
            for r in result:
                r["days_overdue"] = (today - date.fromisoformat(r["retention_until"])).days
            return result

    def create_batch(self, actor_id: str, note: str, archive_ids: list[int], today: date) -> dict:
        """建立处置批次并纳入到期档案，新项一律进入待确认。"""
        if not isinstance(archive_ids, list) or not archive_ids or any(not isinstance(x, int) for x in archive_ids):
            raise BusinessError("archive_ids 必须是非空整数数组", 422, "invalid_archive_ids")
        archive_ids = list(dict.fromkeys(archive_ids))  # 去重保序
        note = (note or "").strip()
        with self.store.connect() as conn:
            self._user(conn, actor_id, {"owner", "archivist"})
            blocker = rules.new_item_blocker()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "INSERT INTO disposition_batches(created_by,note,state,created_at,last_checked_at) VALUES(?,?, 'open', ?, NULL)",
                    (actor_id, note, now()),
                )
                batch_id = cur.lastrowid
                added, skipped = [], []
                for archive_id in archive_ids:
                    archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
                    if not archive:
                        raise BusinessError(f"档案 {archive_id} 不存在", 404, "not_found")
                    if archive["state"] == "disposed":
                        raise BusinessError(f"档案 {archive['name']} 已处置，不能再次纳入", 409, "already_disposed")
                    if not rules.is_expired(archive["retention_until"], today):
                        raise BusinessError(
                            f"档案 {archive['name']} 尚未到期（保留至 {archive['retention_until']}）",
                            422, "not_expired",
                        )
                    dup = conn.execute(
                        "SELECT 1 FROM disposition_items WHERE archive_id=? AND status IN ('pending','ready','frozen')",
                        (archive_id,),
                    ).fetchone()
                    if dup:
                        raise BusinessError(f"档案 {archive['name']} 已在未完结处置批次中", 409, "already_in_batch")
                    conn.execute(
                        """INSERT INTO disposition_items(batch_id,archive_id,status,retention_snapshot,
                               blocker_code,blocker_message,created_by,created_at)
                           VALUES(?,?, 'pending', ?, ?, ?, ?, ?)""",
                        (batch_id, archive_id, archive["retention_until"], blocker["code"], blocker["message"],
                         actor_id, now()),
                    )
                    added.append({"archive_id": archive_id, "name": archive["name"]})
                for added_item in added:
                    self._audit(conn, added_item["archive_id"], actor_id, "disposition.batch_create",
                                {"batch_id": batch_id, "added": added, "note": note})
                batch = conn.execute("SELECT * FROM disposition_batches WHERE id=?", (batch_id,)).fetchone()
                result = self._batch_json(conn, batch, today)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def list_batches(self, user_id: str, today: date) -> list[dict]:
        with self.store.connect() as conn:
            self._user(conn, user_id, {"owner", "archivist", "auditor"})
            batches = conn.execute("SELECT * FROM disposition_batches ORDER BY id DESC").fetchall()
            result = []
            for b in batches:
                d = dict(b)
                counts = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM disposition_items WHERE batch_id=? GROUP BY status",
                    (b["id"],),
                ).fetchall()
                d["counts"] = {k: 0 for k in (rules.PENDING, rules.READY, rules.FROZEN, rules.DISPOSED)}
                for r in counts:
                    d["counts"][r["status"]] = r["n"]
                result.append(d)
            return result

    def get_batch(self, user_id: str, batch_id: int, today: date) -> dict:
        with self.store.connect() as conn:
            self._user(conn, user_id, {"owner", "archivist", "auditor"})
            batch = conn.execute("SELECT * FROM disposition_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise BusinessError("处置批次不存在", 404, "not_found")
            return self._batch_json(conn, batch, today)

    # ------------------------------------------------------------------ 核对 / 冻结
    def _load_item(self, conn, item_id: int) -> sqlite3.Row:
        row = conn.execute(self._ITEM_SELECT + " WHERE i.id=?", (item_id,)).fetchone()
        if not row:
            raise BusinessError("处置项不存在", 404, "not_found")
        if row["archive_state"] == "disposed" or row["status"] == rules.DISPOSED:
            raise BusinessError("该档案已处置", 409, "already_disposed")
        return row

    def recheck_item(self, user_id: str, item_id: int, today: date) -> dict:
        """人工核对：按当前期限与冻结状态确认，可处置则建立新基线。"""
        with self.store.connect() as conn:
            self._user(conn, user_id, {"owner", "archivist", "auditor"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._load_item(conn, item_id)
                decision = rules.review(self._facts(conn, row), today)
                self._persist_decision(conn, row, decision, user_id, "disposition.item_review", {})
                conn.execute("UPDATE disposition_batches SET last_checked_at=? WHERE id=?", (now(), row["batch_id"]))
                result = self._refresh_item(conn, item_id)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def set_freeze(self, actor_id: str, item_id: int, reason: str) -> dict:
        """审计员对有争议的档案做保全冻结。"""
        reason = (reason or "").strip()
        if len(reason) < 2:
            raise BusinessError("冻结理由至少 2 字", 422, "invalid_reason")
        with self.store.connect() as conn:
            self._user(conn, actor_id, {"auditor"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._load_item(conn, item_id)
                if conn.execute("SELECT id FROM freezes WHERE item_id=? AND active=1", (item_id,)).fetchone():
                    raise BusinessError("该处置项已处于保全冻结", 409, "already_frozen")
                cur = conn.execute(
                    "INSERT INTO freezes(item_id,active,reason,created_by,created_at) VALUES(?,?,?,?,?)",
                    (item_id, 1, reason, actor_id, now()),
                )
                blocker = rules._freeze_blocker(rules.Facts(
                    status=rules.FROZEN, retention_snapshot=row["retention_snapshot"],
                    current_retention=row["retention_until"], frozen=True,
                    freeze_reason=reason, frozen_by=actor_id,
                ))
                conn.execute(
                    "UPDATE disposition_items SET status='frozen', blocker_code=?, blocker_message=?, last_checked_at=?, last_checked_by=? WHERE id=?",
                    (blocker["code"], blocker["message"], now(), actor_id, item_id),
                )
                self._audit(conn, row["archive_id"], actor_id, "disposition.freeze",
                            {"item_id": item_id, "batch_id": row["batch_id"], "freeze_id": cur.lastrowid, "reason": reason})
                result = self._refresh_item(conn, item_id)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def release_freeze(self, actor_id: str, item_id: int, note: str) -> dict:
        """审计员解除保全冻结，处置项退回待确认重新核对。"""
        note = (note or "").strip()
        with self.store.connect() as conn:
            self._user(conn, actor_id, {"auditor"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._load_item(conn, item_id)
                freeze = conn.execute("SELECT id FROM freezes WHERE item_id=? AND active=1", (item_id,)).fetchone()
                if not freeze:
                    raise BusinessError("该处置项没有生效中的保全冻结", 409, "not_frozen")
                conn.execute("UPDATE freezes SET active=0, released_by=?, released_at=? WHERE id=?",
                             (actor_id, now(), freeze["id"]))
                blocker = rules.released_blocker()
                conn.execute(
                    "UPDATE disposition_items SET status='pending', blocker_code=?, blocker_message=?, last_checked_at=?, last_checked_by=? WHERE id=?",
                    (blocker["code"], blocker["message"], now(), actor_id, item_id),
                )
                self._audit(conn, row["archive_id"], actor_id, "disposition.freeze_release",
                            {"item_id": item_id, "batch_id": row["batch_id"], "note": note})
                result = self._refresh_item(conn, item_id)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------ 执行
    def execute_batch(self, actor_id: str, batch_id: int, today: date) -> dict:
        """批次执行：执行前闸门逐项核对，有变化即退回待确认，只有未变化的可处置项执行。"""
        with self.store.connect() as conn:
            self._user(conn, actor_id, {"owner", "archivist"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                batch = conn.execute("SELECT * FROM disposition_batches WHERE id=?", (batch_id,)).fetchone()
                if not batch:
                    raise BusinessError("处置批次不存在", 404, "not_found")
                rows = conn.execute(
                    self._ITEM_SELECT + " WHERE i.batch_id=? ORDER BY i.id", (batch_id,)
                ).fetchall()
                disposed, already_disposed, demoted, still_blocked = [], [], [], []
                for row in rows:
                    if row["status"] == rules.DISPOSED:
                        already_disposed.append(row["archive_id"])
                        continue
                    # 执行前闸门：每个未处置项（待确认、可处置、已冻结）都重新核对期限与冻结状态
                    decision = rules.gate(self._facts(conn, row), today)
                    if row["status"] == rules.READY and decision.status == rules.READY and decision.blocker is None:
                        ts = now()
                        conn.execute(
                            "UPDATE disposition_items SET status='disposed', blocker_code=NULL, blocker_message=NULL, disposed_at=?, last_checked_at=?, last_checked_by=? WHERE id=?",
                            (ts, ts, actor_id, row["id"]),
                        )
                        conn.execute("UPDATE archives SET state='disposed' WHERE id=?", (row["archive_id"],))
                        self._audit(conn, row["archive_id"], actor_id, "disposition.execute",
                                    {"item_id": row["id"], "batch_id": batch_id,
                                     "retention_until": row["retention_until"]})
                        disposed.append(row["archive_id"])
                        continue
                    # 可处置项在执行前发现变化 → 退回待确认；其余状态保持原阻塞
                    if row["status"] == rules.READY:
                        self._persist_decision(conn, row, decision, actor_id, "disposition.gate_failed",
                                               {"batch_id": batch_id})
                        demoted.append(row["id"])
                    still_blocked.append({"item_id": row["id"], "blocker": decision.blocker or self._item_json(row)["blocker"]})
                conn.execute("UPDATE disposition_batches SET last_checked_at=?, executed_at=COALESCE(executed_at,?) WHERE id=?",
                             (now(), now() if disposed else None, batch_id))
                # 批次内已无待确认/可处置/已冻结项时收尾
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM disposition_items WHERE batch_id=? AND status IN ('pending','ready','frozen')",
                    (batch_id,),
                ).fetchone()[0]
                if remaining == 0:
                    conn.execute("UPDATE disposition_batches SET state='executed' WHERE id=?", (batch_id,))
                batch = conn.execute("SELECT * FROM disposition_batches WHERE id=?", (batch_id,)).fetchone()
                result = self._batch_json(conn, batch, today)
                result.update({"disposed_archive_ids": disposed, "already_disposed_archive_ids": already_disposed,
                               "returned_item_ids": demoted, "blocked": still_blocked})
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------ 持久化辅助
    def _persist_decision(self, conn, row: sqlite3.Row, decision: rules.Decision, actor: str, action: str, extra: dict) -> None:
        # review 接受当前期限为新基线（snapshot 非空）时，即使仍为待确认也要推进基线
        if not decision.changed and decision.status == row["status"] and decision.snapshot is None:
            return
        snapshot = decision.snapshot or row["retention_snapshot"]
        conn.execute(
            """UPDATE disposition_items
               SET status=?, retention_snapshot=?, blocker_code=?, blocker_message=?,
                   last_checked_at=?, last_checked_by=?
               WHERE id=?""",
            (decision.status, snapshot,
             decision.blocker["code"] if decision.blocker else None,
             decision.blocker["message"] if decision.blocker else None,
             now(), actor, row["id"]),
        )
        self._audit(conn, row["archive_id"], actor, action,
                    {"item_id": row["id"], "batch_id": row["batch_id"],
                     "from_status": row["status"], "to_status": decision.status,
                     **extra})

    def _refresh_item(self, conn, item_id: int) -> dict:
        return self._item_json(conn.execute(self._ITEM_SELECT + " WHERE i.id=?", (item_id,)).fetchone())
