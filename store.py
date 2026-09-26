"""数据层：SQLite 表结构、多副本校验/修复/迁移、到期处置批次与审计。

业务规则的判定放在 rules.py，本模块只负责持久化与事务。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath

from errors import BusinessError
from rules import (
    ALL_STATES, BLOCKER_FROZEN, BATCH_EXECUTED, BATCH_OPEN, DISPOSED, FROZEN,
    GROUPS, PENDING, READY, evaluate_blockers, execution_block, is_due,
    parse_date, recheck_state, state_on_enqueue, state_on_freeze, state_on_unfreeze,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "preservation.db"
MAX_FILE_SIZE = 10 * 1024 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def verify_manifest(files: object) -> list[dict]:
    if not isinstance(files, list) or not files:
        raise BusinessError("files 必须是非空数组", 422, "invalid_manifest")
    result, seen = [], set()
    for item in files:
        if not isinstance(item, dict):
            raise BusinessError("文件条目必须是对象", 422, "invalid_manifest")
        raw_path = str(item.get("path", "")).strip().replace("\\", "/")
        pure = PurePosixPath(raw_path)
        if not raw_path or pure.is_absolute() or ".." in pure.parts or pure.name in {"", ".", ".."}:
            raise BusinessError(f"档案路径不安全: {raw_path}", 422, "unsafe_path")
        if raw_path in seen:
            raise BusinessError(f"档案路径重复: {raw_path}", 409, "duplicate_path")
        seen.add(raw_path)
        encoded = item.get("content_b64")
        if not isinstance(encoded, str):
            raise BusinessError(f"{raw_path} 缺少 content_b64", 422, "content_required")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError(f"{raw_path} 不是合法 Base64", 422, "invalid_base64")
        if len(content) > MAX_FILE_SIZE:
            raise BusinessError(f"{raw_path} 超过单文件大小限制", 413, "file_too_large")
        result.append(
            {"path": raw_path, "content": content, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        )
    return result


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','archivist','auditor'))
);
CREATE TABLE IF NOT EXISTS archives(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL REFERENCES users(id),
    retention_until TEXT NOT NULL,
    restricted INTEGER NOT NULL DEFAULT 1 CHECK(restricted IN (0,1)),
    disposed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_members(
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    permission TEXT NOT NULL CHECK(permission IN ('read','write')),
    PRIMARY KEY(archive_id,user_id)
);
CREATE TABLE IF NOT EXISTS archive_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    version INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'verified' CHECK(state IN ('verified','degraded')),
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE(archive_id,version)
);
CREATE TABLE IF NOT EXISTS archive_files(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    content BLOB NOT NULL,
    UNIQUE(version_id,path)
);
CREATE TABLE IF NOT EXISTS copies(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    location TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'healthy' CHECK(state IN ('healthy','corrupt','degraded')),
    created_at TEXT NOT NULL,
    last_verified_at TEXT,
    UNIQUE(version_id,location)
);
CREATE TABLE IF NOT EXISTS copy_files(
    copy_id INTEGER NOT NULL REFERENCES copies(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    content BLOB NOT NULL,
    PRIMARY KEY(copy_id,path)
);
CREATE TABLE IF NOT EXISTS migrations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    target_version_id INTEGER NOT NULL UNIQUE REFERENCES archive_versions(id),
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    target_format TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disposition_batches(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','executed')),
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    executed_at TEXT
);
CREATE TABLE IF NOT EXISTS disposition_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES disposition_batches(id),
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    state TEXT NOT NULL CHECK(state IN ('pending','ready','frozen','disposed')),
    retention_snapshot TEXT,
    blockers TEXT NOT NULL DEFAULT '[]',
    added_by TEXT NOT NULL REFERENCES users(id),
    added_at TEXT NOT NULL,
    confirmed_at TEXT,
    checked_at TEXT,
    disposed_at TEXT,
    UNIQUE(batch_id,archive_id)
);
CREATE TABLE IF NOT EXISTS freezes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    released_at TEXT,
    released_by TEXT REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_freezes_active ON freezes(archive_id, active);
CREATE INDEX IF NOT EXISTS idx_items_state ON disposition_items(state);
"""


class PreservationStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(_SCHEMA)
            # 兼容旧库：补齐 archives.disposed_at
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(archives)").fetchall()}
            if "disposed_at" not in cols:
                conn.execute("ALTER TABLE archives ADD COLUMN disposed_at TEXT")

    def seed(self) -> None:
        """写入演示用户与三档不同到期状态的演示档案，便于直接体验处置台。"""
        self.init_schema()
        import base64 as _b64
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("owner", "机构档案负责人", "owner"),
                    ("archivist", "档案管理员", "archivist"),
                    ("auditor", "独立审计员", "auditor"),
                    ("outsider", "未授权访客", "auditor"),
                ],
            )

            def _seed_archive(name: str, retention: str, actor: str = "archivist", write: bool = True) -> int:
                row = conn.execute("SELECT id FROM archives WHERE name=?", (name,)).fetchone()
                if row:
                    return row["id"]
                cur = conn.execute(
                    "INSERT INTO archives(name,owner_id,retention_until,restricted,created_at) VALUES(?,?,?,?,?)",
                    (name, "owner", retention, 1, now()),
                )
                archive_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,'write')",
                    (archive_id, "owner"),
                )
                if write:
                    conn.execute(
                        "INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,'write')",
                        (archive_id, actor),
                    )
                conn.execute(
                    "INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,'read')",
                    (archive_id, "auditor"),
                )
                content = f"seed content of {name}".encode()
                ver = conn.execute(
                    "INSERT INTO archive_versions(archive_id,version,created_by,created_at) VALUES(?,1,?,?)",
                    (archive_id, actor, now()),
                ).lastrowid
                conn.execute(
                    "INSERT INTO archive_files(version_id,path,sha256,size,content) VALUES(?,?,?,?,?)",
                    (ver, "doc.txt", hashlib.sha256(content).hexdigest(), len(content), content),
                )
                copy = conn.execute(
                    "INSERT INTO copies(version_id,location,created_at,last_verified_at) VALUES(?,?,?,?)",
                    (ver, "offline-disk-a", now(), now()),
                ).lastrowid
                conn.execute(
                    "INSERT INTO copy_files(copy_id,path,sha256,size,content) VALUES(?,?,?,?,?)",
                    (copy, "doc.txt", hashlib.sha256(content).hexdigest(), len(content), content),
                )
                conn.execute(
                    "INSERT INTO audit_log(archive_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                    (archive_id, actor, "archive.create",
                     json.dumps({"retention_until": retention, "restricted": True, "seed": True}, ensure_ascii=False), now()),
                )
                return archive_id

            today = date.today()
            _seed_archive("已到期-测绘底图", (today.replace(year=today.year - 1)).isoformat())
            _seed_archive("已到期-竣工图纸", (today.replace(year=today.year - 2)).isoformat())
            _seed_archive("未到期-年度报告", (today.replace(year=today.year + 3)).isoformat(), write=False)
            # 演示账号 auditor 对 seed 数据可读；outsider 不是任何档案成员

    # ------------------------------------------------------------------
    # 鉴权与通用
    # ------------------------------------------------------------------
    def _user(self, conn, user_id: str | None, roles: set[str] | None = None) -> sqlite3.Row:
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _access(self, conn, archive_id: int, user: sqlite3.Row, require_write: bool = False) -> None:
        archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
        if not archive:
            raise BusinessError("档案不存在", 404, "not_found")
        if archive["owner_id"] == user["id"]:
            return
        row = conn.execute(
            "SELECT permission FROM archive_members WHERE archive_id=? AND user_id=?", (archive_id, user["id"])
        ).fetchone()
        if not row or (require_write and row["permission"] != "write"):
            raise BusinessError("没有该受限档案的访问权限", 403, "forbidden")

    def _can_access(self, conn, archive_id: int, user: sqlite3.Row) -> bool:
        """_access 的不抛异常版本，用于列表过滤。"""
        archive = conn.execute("SELECT owner_id FROM archives WHERE id=?", (archive_id,)).fetchone()
        if not archive:
            return False
        if archive["owner_id"] == user["id"]:
            return True
        return conn.execute(
            "SELECT 1 FROM archive_members WHERE archive_id=? AND user_id=?", (archive_id, user["id"])
        ).fetchone() is not None

    def _require_writable_archive(self, archive: sqlite3.Row) -> None:
        if archive["disposed_at"]:
            raise BusinessError("档案已到期处置，记录只读", 409, "archive_disposed")

    def _audit(self, conn, archive_id: int, actor: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(archive_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (archive_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _active_freeze(self, conn, archive_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM freezes WHERE archive_id=? AND active=1 ORDER BY id DESC LIMIT 1", (archive_id,)
        ).fetchone()

    # ------------------------------------------------------------------
    # 档案与版本（原有能力）
    # ------------------------------------------------------------------
    def create_archive(self, user_id: str, name: str, retention_until: str, restricted: bool = True) -> dict:
        name = name.strip()
        if len(name) < 2:
            raise BusinessError("档案名称至少 2 字", 422, "invalid_name")
        try:
            deadline = date.fromisoformat(retention_until)
        except ValueError:
            raise BusinessError("retention_until 必须是 YYYY-MM-DD", 422, "invalid_retention")
        if deadline < date.today():
            raise BusinessError("保留期限不能早于今天", 422, "retention_in_past")
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist"})
            try:
                cur = conn.execute(
                    "INSERT INTO archives(name,owner_id,retention_until,restricted,created_at) VALUES(?,?,?,?,?)",
                    (name, user_id, retention_until, int(bool(restricted)), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("档案名称已存在", 409, "archive_exists")
            archive_id = cur.lastrowid
            conn.execute(
                "INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,'write')", (archive_id, user_id)
            )
            self._audit(conn, archive_id, user_id, "archive.create", {"retention_until": retention_until, "restricted": restricted})
            return {"id": archive_id, "name": name, "retention_until": retention_until, "restricted": restricted}

    def update_retention(self, actor_id: str, archive_id: int, retention_until: str) -> dict:
        """延长/调整保留期限；只能向后调整，处置台据此退回已确认条目。"""
        try:
            deadline = date.fromisoformat(retention_until)
        except ValueError:
            raise BusinessError("retention_until 必须是 YYYY-MM-DD", 422, "invalid_retention")
        with self.connect() as conn:
            actor = self._user(conn, actor_id, {"owner", "archivist"})
            self._access(conn, archive_id, actor, require_write=True)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
            self._require_writable_archive(archive)
            old = archive["retention_until"]
            if deadline <= date.fromisoformat(old):
                raise BusinessError("只能将保留期限调整到更晚的日期", 422, "retention_must_extend")
            conn.execute("UPDATE archives SET retention_until=? WHERE id=?", (retention_until, archive_id))
            # 打开批次中的 ready 条目立即退回待确认，快照不匹配由执行前核对兜底
            conn.execute(
                "UPDATE disposition_items SET state='pending',checked_at=NULL,confirmed_at=NULL "
                "WHERE archive_id=? AND state='ready'",
                (archive_id,),
            )
            rows = conn.execute(
                "SELECT batch_id FROM disposition_items WHERE archive_id=? AND state<>'disposed'", (archive_id,)
            ).fetchall()
            for r in rows:
                conn.execute("UPDATE disposition_batches SET last_checked_at=? WHERE id=?", (now(), r["batch_id"]))
            self._audit(conn, archive_id, actor_id, "retention.update",
                        {"old": old, "new": retention_until, "affected_open_items": len(rows)})
            return {"archive_id": archive_id, "retention_until": retention_until, "previous": old}

    def grant(self, actor_id: str, archive_id: int, user_id: str, permission: str) -> dict:
        if permission not in {"read", "write"}:
            raise BusinessError("permission 必须是 read 或 write", 422, "invalid_permission")
        with self.connect() as conn:
            actor = self._user(conn, actor_id)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
            if not archive:
                raise BusinessError("档案不存在", 404, "not_found")
            if archive["owner_id"] != actor_id:
                raise BusinessError("只有档案所有者可以授权", 403, "forbidden")
            self._user(conn, user_id)
            conn.execute(
                """INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,?)
                   ON CONFLICT(archive_id,user_id) DO UPDATE SET permission=excluded.permission""",
                (archive_id, user_id, permission),
            )
            self._audit(conn, archive_id, actor_id, "access.grant", {"user_id": user_id, "permission": permission})
            return {"archive_id": archive_id, "user_id": user_id, "permission": permission}

    def ingest_version(self, actor_id: str, archive_id: int, files: object) -> dict:
        manifest = verify_manifest(files)
        with self.connect() as conn:
            actor = self._user(conn, actor_id, {"owner", "archivist"})
            self._access(conn, archive_id, actor, require_write=True)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
            self._require_writable_archive(archive)
            try:
                conn.execute("BEGIN IMMEDIATE")
                version_no = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM archive_versions WHERE archive_id=?", (archive_id,)
                ).fetchone()[0]
                cur = conn.execute(
                    "INSERT INTO archive_versions(archive_id,version,created_by,created_at) VALUES(?,?,?,?)",
                    (archive_id, version_no, actor_id, now()),
                )
                version_id = cur.lastrowid
                for item in manifest:
                    conn.execute(
                        "INSERT INTO archive_files(version_id,path,sha256,size,content) VALUES(?,?,?,?,?)",
                        (version_id, item["path"], item["sha256"], item["size"], item["content"]),
                    )
                self._audit(
                    conn, archive_id, actor_id, "version.ingest",
                    {"version_id": version_id, "version": version_no, "files": len(manifest),
                     "manifest": [{"path": x["path"], "sha256": x["sha256"], "size": x["size"]} for x in manifest]},
                )
                return {"id": version_id, "archive_id": archive_id, "version": version_no, "file_count": len(manifest)}
            except Exception:
                conn.rollback()
                raise

    def add_copy(self, actor_id: str, version_id: int, location: str) -> dict:
        location = location.strip()
        if len(location) < 2:
            raise BusinessError("副本位置不能为空", 422, "invalid_location")
        with self.connect() as conn:
            actor = self._user(conn, actor_id, {"owner", "archivist"})
            version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise BusinessError("档案版本不存在", 404, "not_found")
            self._access(conn, version["archive_id"], actor, require_write=True)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (version["archive_id"],)).fetchone()
            self._require_writable_archive(archive)
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "INSERT INTO copies(version_id,location,created_at,last_verified_at) VALUES(?,?,?,?)",
                    (version_id, location, now(), now()),
                )
                copy_id = cur.lastrowid
                conn.execute(
                    """INSERT INTO copy_files(copy_id,path,sha256,size,content)
                       SELECT ?,path,sha256,size,content FROM archive_files WHERE version_id=?""",
                    (copy_id, version_id),
                )
                self._audit(conn, version["archive_id"], actor_id, "copy.create", {"copy_id": copy_id, "version_id": version_id, "location": location})
                return {"id": copy_id, "version_id": version_id, "location": location, "state": "healthy"}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("该版本的副本位置已存在", 409, "copy_exists")
            except Exception:
                conn.rollback()
                raise

    def get_version(self, user_id: str, version_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist", "auditor"})
            version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise BusinessError("档案版本不存在", 404, "not_found")
            self._access(conn, version["archive_id"], user)
            files = conn.execute(
                "SELECT path,sha256,size FROM archive_files WHERE version_id=? ORDER BY path", (version_id,)
            ).fetchall()
            copies = conn.execute(
                "SELECT id,location,state,last_verified_at FROM copies WHERE version_id=? ORDER BY id", (version_id,)
            ).fetchall()
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (version["archive_id"],)).fetchone()
            return {"version": dict(version), "archive": dict(archive), "files": [dict(x) for x in files], "copies": [dict(x) for x in copies]}

    def verify_copy(self, user_id: str, copy_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist", "auditor"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                copy = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
                if not copy:
                    raise BusinessError("副本不存在", 404, "not_found")
                version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (copy["version_id"],)).fetchone()
                self._access(conn, version["archive_id"], user)
                stored = conn.execute(
                    "SELECT path,sha256,size,content FROM copy_files WHERE copy_id=? ORDER BY path", (copy_id,)
                ).fetchall()
                corrupt_paths = [r["path"] for r in stored if hashlib.sha256(r["content"]).hexdigest() != r["sha256"] or len(r["content"]) != r["size"]]
                repaired = False
                if not corrupt_paths:
                    conn.execute("UPDATE copies SET state='healthy',last_verified_at=? WHERE id=?", (now(), copy_id))
                    result_state = "healthy"
                else:
                    conn.execute("UPDATE copies SET state='corrupt',last_verified_at=? WHERE id=?", (now(), copy_id))
                    healthy = conn.execute(
                        "SELECT id FROM copies WHERE version_id=? AND id<>? AND state='healthy' ORDER BY last_verified_at DESC LIMIT 1",
                        (copy["version_id"], copy_id),
                    ).fetchone()
                    result_state = "degraded"
                    if healthy:
                        donor = conn.execute(
                            "SELECT path,sha256,size,content FROM copy_files WHERE copy_id=? ORDER BY path", (healthy["id"],)
                        ).fetchall()
                        donor_by_path = {r["path"]: r for r in donor}
                        expected = {r["path"]: r for r in conn.execute(
                            "SELECT path,sha256,size FROM archive_files WHERE version_id=?", (copy["version_id"],)
                        ).fetchall()}
                        if set(donor_by_path) == set(expected) and all(
                            hashlib.sha256(donor_by_path[p]["content"]).hexdigest() == expected[p]["sha256"] for p in expected
                        ):
                            conn.execute("DELETE FROM copy_files WHERE copy_id=?", (copy_id,))
                            conn.execute(
                                """INSERT INTO copy_files(copy_id,path,sha256,size,content)
                                   SELECT ?,path,sha256,size,content FROM copy_files WHERE copy_id=?""",
                                (copy_id, healthy["id"]),
                            )
                            conn.execute("UPDATE copies SET state='healthy',last_verified_at=? WHERE id=?", (now(), copy_id))
                            repaired, result_state = True, "healthy"
                    if result_state == "degraded":
                        conn.execute("UPDATE archive_versions SET state='degraded' WHERE id=?", (copy["version_id"],))
                self._audit(
                    conn, version["archive_id"], user_id, "copy.verify",
                    {"copy_id": copy_id, "state": result_state, "corrupt_paths": corrupt_paths, "repaired": repaired},
                )
                return {"copy_id": copy_id, "state": result_state, "corrupt_paths": corrupt_paths, "repaired": repaired}
            except Exception:
                conn.rollback()
                raise

    def simulate_corruption(self, user_id: str, copy_id: int, path: str) -> dict:
        """仅用于演示和测试，在受控环境中模拟底层介质损坏。"""
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist"})
            copy = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
            if not copy:
                raise BusinessError("副本不存在", 404, "not_found")
            version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (copy["version_id"],)).fetchone()
            self._access(conn, version["archive_id"], user, require_write=True)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (version["archive_id"],)).fetchone()
            self._require_writable_archive(archive)
            row = conn.execute("SELECT content FROM copy_files WHERE copy_id=? AND path=?", (copy_id, path)).fetchone()
            if not row:
                raise BusinessError("副本文件不存在", 404, "not_found")
            damaged = bytes([row["content"][0] ^ 0xFF]) + row["content"][1:] if row["content"] else b"corrupt"
            conn.execute("UPDATE copy_files SET content=? WHERE copy_id=? AND path=?", (damaged, copy_id, path))
            conn.execute("UPDATE copies SET state='corrupt' WHERE id=?", (copy_id,))
            self._audit(conn, version["archive_id"], user_id, "copy.simulate_corruption", {"copy_id": copy_id, "path": path})
            return {"copy_id": copy_id, "path": path, "state": "corrupt"}

    def migrate(self, actor_id: str, version_id: int, source_path: str, target_path: str, target_format: str, content_b64: str) -> dict:
        with self.connect() as conn:
            actor = self._user(conn, actor_id, {"owner", "archivist"})
            source_version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
            if not source_version:
                raise BusinessError("源档案版本不存在", 404, "not_found")
            self._access(conn, source_version["archive_id"], actor, require_write=True)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (source_version["archive_id"],)).fetchone()
            self._require_writable_archive(archive)
            source = conn.execute(
                "SELECT * FROM archive_files WHERE version_id=? AND path=?", (version_id, source_path)
            ).fetchone()
            if not source:
                raise BusinessError("源文件不存在", 404, "source_not_found")
            converted = verify_manifest([{"path": target_path, "content_b64": content_b64}])[0]
            try:
                conn.execute("BEGIN IMMEDIATE")
                version_no = conn.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM archive_versions WHERE archive_id=?", (source_version["archive_id"],)
                ).fetchone()[0]
                cur = conn.execute(
                    "INSERT INTO archive_versions(archive_id,version,created_by,created_at) VALUES(?,?,?,?)",
                    (source_version["archive_id"], version_no, actor_id, now()),
                )
                target_version_id = cur.lastrowid
                conn.execute(
                    """INSERT INTO archive_files(version_id,path,sha256,size,content)
                       SELECT ?,path,sha256,size,content FROM archive_files
                       WHERE version_id=? AND path<>?""",
                    (target_version_id, version_id, source_path),
                )
                conn.execute(
                    "INSERT INTO archive_files(version_id,path,sha256,size,content) VALUES(?,?,?,?,?)",
                    (target_version_id, converted["path"], converted["sha256"], converted["size"], converted["content"]),
                )
                conn.execute(
                    "INSERT INTO migrations(source_version_id,target_version_id,source_path,target_path,target_format,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (version_id, target_version_id, source_path, converted["path"], target_format.strip(), actor_id, now()),
                )
                self._audit(
                    conn, source_version["archive_id"], actor_id, "format.migrate",
                    {"source_version_id": version_id, "target_version_id": target_version_id,
                     "source_path": source_path, "target_path": converted["path"], "target_format": target_format.strip()},
                )
                return {"id": target_version_id, "version": version_no, "source_version_id": version_id, "target_path": converted["path"]}
            except Exception:
                conn.rollback()
                raise

    def archive_status(self, user_id: str, archive_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist", "auditor"})
            self._access(conn, archive_id, user)
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
            versions = conn.execute("SELECT id,version,state,created_at FROM archive_versions WHERE archive_id=? ORDER BY version", (archive_id,)).fetchall()
            deadline = date.fromisoformat(archive["retention_until"])
            freeze = self._active_freeze(conn, archive_id)
            item = conn.execute(
                """SELECT di.*, db.name AS batch_name FROM disposition_items di
                   JOIN disposition_batches db ON db.id=di.batch_id
                   WHERE di.archive_id=? AND di.state<>'disposed' ORDER BY di.id DESC LIMIT 1""",
                (archive_id,),
            ).fetchone()
            return {
                "archive": dict(archive),
                "days_remaining": (deadline - date.today()).days,
                "frozen": bool(freeze),
                "freeze_reason": freeze["reason"] if freeze else None,
                "disposition": None if item is None else {
                    "batch_id": item["batch_id"], "batch_name": item["batch_name"],
                    "state": item["state"], "blockers": json.loads(item["blockers"]),
                },
                "versions": [dict(v) | {"file_count": conn.execute("SELECT COUNT(*) FROM archive_files WHERE version_id=?", (v["id"],)).fetchone()[0],
                                         "copy_count": conn.execute("SELECT COUNT(*) FROM copies WHERE version_id=?", (v["id"],)).fetchone()[0]}
                             for v in versions],
                "audit": [dict(r) | {"detail": json.loads(r["detail"])} for r in conn.execute("SELECT * FROM audit_log WHERE archive_id=? ORDER BY id", (archive_id,)).fetchall()],
            }

    # ------------------------------------------------------------------
    # 到期处置台
    # ------------------------------------------------------------------
    def _get_open_batch(self, conn, batch_id: int) -> sqlite3.Row:
        batch = conn.execute("SELECT * FROM disposition_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise BusinessError("处置批次不存在", 404, "not_found")
        if batch["state"] != BATCH_OPEN:
            raise BusinessError("处置批次已执行，不能再修改", 409, "batch_executed")
        return batch

    def _archive_ids(self, archive_ids: object) -> list[int]:
        if not isinstance(archive_ids, list) or not archive_ids:
            raise BusinessError("archive_ids 必须是非空数组", 422, "invalid_archive_ids")
        ids: list[int] = []
        for raw in archive_ids:
            if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
                raise BusinessError("档案 ID 必须是正整数", 422, "invalid_archive_ids")
            ids.append(raw)
        if len(set(ids)) != len(ids):
            raise BusinessError("档案 ID 不能重复", 422, "duplicate_archive")
        return ids

    def _enqueue(self, conn, batch_id: int, archive_id: int, actor_id: str, today: date) -> dict:
        archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
        if not archive:
            raise BusinessError(f"档案 {archive_id} 不存在", 404, "archive_not_found")
        if archive["disposed_at"]:
            raise BusinessError(f"档案 {archive['name']} 已处置，不能纳入批次", 409, "archive_disposed")
        if conn.execute(
            "SELECT 1 FROM disposition_items WHERE batch_id=? AND archive_id=?", (batch_id, archive_id)
        ).fetchone():
            raise BusinessError(f"档案 {archive['name']} 已在该批次中", 409, "already_in_batch")
        frozen = self._active_freeze(conn, archive_id) is not None
        due = is_due(archive["retention_until"], today)
        state = state_on_enqueue(frozen=frozen, due=due)
        # 纳入时若未到期则从未确认过，期限变化不构成额外阻塞
        blockers = evaluate_blockers(
            frozen=frozen, due=due, retention_changed=False,
            already_disposed=False, confirmed=(state == READY),
        )
        conn.execute(
            """INSERT INTO disposition_items
               (batch_id,archive_id,state,retention_snapshot,blockers,added_by,added_at,checked_at,confirmed_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (batch_id, archive_id, state, archive["retention_until"], json.dumps(blockers),
             actor_id, now(), now(), now() if state == READY else None),
        )
        self._audit(conn, archive_id, actor_id, "disposition.enqueue",
                    {"batch_id": batch_id, "state": state, "blockers": blockers,
                     "retention_snapshot": archive["retention_until"]})
        return {"archive_id": archive_id, "state": state, "blockers": blockers}

    def list_due_candidates(self, user_id: str) -> dict:
        """可纳入批次的候选：已到期、未处置、尚未进入任何打开批次。"""
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist", "auditor"})
            today = date.today()
            rows = conn.execute(
                """SELECT a.* FROM archives a
                   WHERE a.disposed_at IS NULL AND date(a.retention_until)<=date(?)
                   AND NOT EXISTS(
                       SELECT 1 FROM disposition_items di
                       JOIN disposition_batches db ON db.id=di.batch_id
                       WHERE di.archive_id=a.id AND db.state='open' AND di.state<>'disposed')
                   ORDER BY a.retention_until, a.id""",
                (today.isoformat(),),
            ).fetchall()
            items = []
            for r in rows:
                # 无访问权限的档案直接过滤，不暴露其存在
                if not self._can_access(conn, r["id"], user):
                    continue
                items.append({
                    "archive_id": r["id"], "name": r["name"],
                    "retention_until": r["retention_until"],
                    "days_overdue": (today - parse_date(r["retention_until"])).days,
                    "frozen": self._active_freeze(conn, r["id"]) is not None,
                })
            return {"today": today.isoformat(), "candidates": items}

    def create_batch(self, actor_id: str, name: str, archive_ids: object) -> dict:
        with self.connect() as conn:
            self._user(conn, actor_id, {"owner", "archivist"})
        name = str(name or "").strip()
        if len(name) < 2:
            raise BusinessError("批次名称至少 2 字", 422, "invalid_batch_name")
        ids = self._archive_ids(archive_ids)
        with self.connect() as conn:
            today = date.today()
            try:
                conn.execute("BEGIN IMMEDIATE")
                batch_id = conn.execute(
                    "INSERT INTO disposition_batches(name,state,created_by,created_at,last_checked_at) VALUES(?, 'open', ?,?,?)",
                    (name, actor_id, now(), now()),
                ).lastrowid
                enqueued = [self._enqueue(conn, batch_id, aid, actor_id, today) for aid in ids]
                return self.batch_detail(actor_id, batch_id, conn=conn)
            except Exception:
                conn.rollback()
                raise

    def add_archives(self, actor_id: str, batch_id: int, archive_ids: object) -> dict:
        with self.connect() as conn:
            self._user(conn, actor_id, {"owner", "archivist"})
        ids = self._archive_ids(archive_ids)
        with self.connect() as conn:
            self._get_open_batch(conn, batch_id)
            today = date.today()
            try:
                conn.execute("BEGIN IMMEDIATE")
                enqueued = [self._enqueue(conn, batch_id, aid, actor_id, today) for aid in ids]
                conn.execute("UPDATE disposition_batches SET last_checked_at=? WHERE id=?", (now(), batch_id))
                return self.batch_detail(actor_id, batch_id, conn=conn)
            except Exception:
                conn.rollback()
                raise

    def remove_item(self, actor_id: str, batch_id: int, item_id: int) -> dict:
        with self.connect() as conn:
            self._user(conn, actor_id, {"owner", "archivist"})
            self._get_open_batch(conn, batch_id)
            item = conn.execute(
                "SELECT * FROM disposition_items WHERE id=? AND batch_id=?", (item_id, batch_id)
            ).fetchone()
            if not item:
                raise BusinessError("批次条目不存在", 404, "not_found")
            if item["state"] == FROZEN:
                raise BusinessError("已冻结条目须先由审计员解冻才能移出", 409, "item_frozen")
            conn.execute("DELETE FROM disposition_items WHERE id=?", (item_id,))
            self._audit(conn, item["archive_id"], actor_id, "disposition.remove",
                        {"batch_id": batch_id, "item_id": item_id})
            return self.batch_detail(actor_id, batch_id, conn=conn)

    def freeze_item(self, actor_id: str, batch_id: int, item_id: int, reason: str) -> dict:
        """审计员对有争议档案做保全冻结。"""
        with self.connect() as conn:
            self._user(conn, actor_id, {"auditor"})
            reason = str(reason or "").strip()
            if len(reason) < 2:
                raise BusinessError("冻结理由至少 2 字", 422, "invalid_reason")
            self._get_open_batch(conn, batch_id)
            item = conn.execute(
                "SELECT * FROM disposition_items WHERE id=? AND batch_id=?", (item_id, batch_id)
            ).fetchone()
            if not item:
                raise BusinessError("批次条目不存在", 404, "not_found")
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (item["archive_id"],)).fetchone()
            self._access(conn, archive["id"], conn.execute("SELECT * FROM users WHERE id=?", (actor_id,)).fetchone())
            if item["state"] == DISPOSED:
                raise BusinessError("档案已处置，不能冻结", 409, "item_disposed")
            if self._active_freeze(conn, archive["id"]):
                raise BusinessError("该档案已在冻结中", 409, "freeze_exists")
            conn.execute(
                "INSERT INTO freezes(archive_id,reason,active,created_by,created_at) VALUES(?, ?,1,?,?)",
                (archive["id"], reason, actor_id, now()),
            )
            new_state = state_on_freeze(item["state"])
            blockers = evaluate_blockers(
                frozen=True,
                due=is_due(archive["retention_until"], date.today()),
                retention_changed=item["retention_snapshot"] != archive["retention_until"],
                already_disposed=False,
                confirmed=bool(item["confirmed_at"]),
            )
            conn.execute(
                "UPDATE disposition_items SET state=?,blockers=?,checked_at=? WHERE id=?",
                (new_state, json.dumps(blockers), now(), item_id),
            )
            conn.execute("UPDATE disposition_batches SET last_checked_at=? WHERE id=?", (now(), batch_id))
            self._audit(conn, archive["id"], actor_id, "disposition.freeze",
                        {"batch_id": batch_id, "item_id": item_id, "reason": reason,
                         "previous_state": item["state"], "state": new_state})
            return self.batch_detail(actor_id, batch_id, conn=conn)

    def unfreeze_item(self, actor_id: str, batch_id: int, item_id: int) -> dict:
        with self.connect() as conn:
            self._user(conn, actor_id, {"auditor"})
            self._get_open_batch(conn, batch_id)
            item = conn.execute(
                "SELECT * FROM disposition_items WHERE id=? AND batch_id=?", (item_id, batch_id)
            ).fetchone()
            if not item:
                raise BusinessError("批次条目不存在", 404, "not_found")
            freeze = self._active_freeze(conn, item["archive_id"])
            if not freeze:
                raise BusinessError("该档案没有生效中的冻结", 409, "no_active_freeze")
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (item["archive_id"],)).fetchone()
            self._access(conn, archive["id"], conn.execute("SELECT * FROM users WHERE id=?", (actor_id,)).fetchone())
            conn.execute(
                "UPDATE freezes SET active=0,released_at=?,released_by=? WHERE id=?",
                (now(), actor_id, freeze["id"]),
            )
            due = is_due(archive["retention_until"], date.today())
            retention_changed = item["retention_snapshot"] != archive["retention_until"]
            new_state = state_on_unfreeze(
                state=item["state"], due=due, retention_changed=retention_changed,
                confirmed=bool(item["confirmed_at"]),
            )
            blockers = evaluate_blockers(
                frozen=False, due=due, retention_changed=retention_changed,
                already_disposed=False, confirmed=bool(item["confirmed_at"]),
            )
            conn.execute(
                "UPDATE disposition_items SET state=?,blockers=?,checked_at=?,confirmed_at=? WHERE id=?",
                (new_state, json.dumps(blockers), now(), now() if new_state == READY else None, item_id),
            )
            conn.execute("UPDATE disposition_batches SET last_checked_at=? WHERE id=?", (now(), batch_id))
            self._audit(conn, archive["id"], actor_id, "disposition.unfreeze",
                        {"batch_id": batch_id, "item_id": item_id, "state": new_state,
                         "blockers": blockers, "previous_reason": freeze["reason"]})
            return self.batch_detail(actor_id, batch_id, conn=conn)

    def _recheck(self, conn, batch_id: int, actor_id: str, today: date) -> dict:
        """核对核心：对每条未处置条目重算期限与冻结事实，有变化即退回待确认。"""
        items = conn.execute(
            "SELECT * FROM disposition_items WHERE batch_id=? AND state<>? ORDER BY id",
            (batch_id, DISPOSED),
        ).fetchall()
        changed = []
        for item in items:
            archive = conn.execute("SELECT * FROM archives WHERE id=?", (item["archive_id"],)).fetchone()
            frozen_row = self._active_freeze(conn, item["archive_id"])
            frozen = frozen_row is not None
            due = is_due(archive["retention_until"], today)
            retention_changed = item["retention_snapshot"] != archive["retention_until"]
            blockers = evaluate_blockers(
                frozen=frozen, due=due, retention_changed=retention_changed,
                already_disposed=False, confirmed=bool(item["confirmed_at"]),
            )
            new_state = recheck_state(state=item["state"], blockers=blockers)
            # 有阻塞的 ready/pending 一律停留在待确认；冻结态由冻结/解冻动作维护，
            # 但核对发现新增冻结时也要立刻反映为 frozen。
            if frozen and item["state"] != FROZEN:
                new_state = FROZEN
            if not frozen and item["state"] == FROZEN:
                new_state = recheck_state(state=PENDING, blockers=blockers)
            if new_state != READY:
                # 退回待确认意味着管理员需要重新确认
                conn.execute(
                    "UPDATE disposition_items SET confirmed_at=NULL WHERE id=? AND state<>'frozen'", (item["id"],)
                )
            if new_state != item["state"] or json.loads(item["blockers"]) != blockers:
                conn.execute(
                    "UPDATE disposition_items SET state=?,blockers=?,checked_at=? WHERE id=?",
                    (new_state, json.dumps(blockers), now(), item["id"]),
                )
                changed.append({
                    "item_id": item["id"], "archive_id": item["archive_id"],
                    "previous_state": item["state"], "state": new_state, "blockers": blockers,
                })
                self._audit(conn, item["archive_id"], actor_id, "disposition.recheck_item",
                            {"batch_id": batch_id, "previous_state": item["state"],
                             "state": new_state, "blockers": blockers,
                             "retention_snapshot": item["retention_snapshot"],
                             "retention_current": archive["retention_until"], "frozen": frozen})
        conn.execute("UPDATE disposition_batches SET last_checked_at=? WHERE id=?", (now(), batch_id))
        return {"changed": changed}

    def check_batch(self, actor_id: str, batch_id: int) -> dict:
        with self.connect() as conn:
            self._user(conn, actor_id, {"owner", "archivist", "auditor"})
            self._get_open_batch(conn, batch_id)
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = self._recheck(conn, batch_id, actor_id, date.today())
                detail = self.batch_detail(actor_id, batch_id, conn=conn)
                result["batch"] = detail
                return result
            except Exception:
                conn.rollback()
                raise

    def confirm_items(self, actor_id: str, batch_id: int, item_ids: object) -> dict:
        """管理员确认：核对无变化（无阻塞）才允许标记可处置。"""
        with self.connect() as conn:
            self._user(conn, actor_id, {"owner", "archivist"})
        if not isinstance(item_ids, list) or not item_ids or not all(isinstance(x, int) and x > 0 for x in item_ids):
            raise BusinessError("item_ids 必须是非空正整数数组", 422, "invalid_item_ids")
        with self.connect() as conn:
            self._get_open_batch(conn, batch_id)
            today = date.today()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rejected = []
                for item_id in item_ids:
                    item = conn.execute(
                        "SELECT * FROM disposition_items WHERE id=? AND batch_id=?", (item_id, batch_id)
                    ).fetchone()
                    if not item:
                        raise BusinessError(f"批次条目 {item_id} 不存在", 404, "not_found")
                    archive = conn.execute("SELECT * FROM archives WHERE id=?", (item["archive_id"],)).fetchone()
                    blockers = evaluate_blockers(
                        frozen=self._active_freeze(conn, item["archive_id"]) is not None,
                        due=is_due(archive["retention_until"], today),
                        retention_changed=item["retention_snapshot"] != archive["retention_until"],
                        already_disposed=False,
                        confirmed=bool(item["confirmed_at"]),
                    )
                    if blockers:
                        rejected.append({"item_id": item_id, "archive_id": item["archive_id"], "blockers": blockers})
                        conn.execute(
                            "UPDATE disposition_items SET state='pending',blockers=?,checked_at=? WHERE id=?",
                            (json.dumps(blockers), now(), item_id),
                        )
                        self._audit(conn, item["archive_id"], actor_id, "disposition.confirm_rejected",
                                    {"batch_id": batch_id, "item_id": item_id, "blockers": blockers})
                        continue
                    conn.execute(
                        "UPDATE disposition_items SET state='ready',blockers='[]',checked_at=?,confirmed_at=? WHERE id=?",
                        (now(), now(), item_id),
                    )
                    self._audit(conn, item["archive_id"], actor_id, "disposition.confirm",
                                {"batch_id": batch_id, "item_id": item_id})
                conn.execute("UPDATE disposition_batches SET last_checked_at=? WHERE id=?", (now(), batch_id))
                detail = self.batch_detail(actor_id, batch_id, conn=conn)
                detail["rejected"] = rejected
                return detail
            except Exception:
                conn.rollback()
                raise

    def execute_batch(self, actor_id: str, batch_id: int) -> dict:
        """执行处置：执行前最后核对期限与冻结状态，任一变化即整体退回待确认。"""
        with self.connect() as conn:
            self._user(conn, actor_id, {"owner", "archivist"})
            self._get_open_batch(conn, batch_id)
        # 第一步：核对结果（退回待确认）独立落库，即使随后拒绝执行也要保留
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._recheck(conn, batch_id, actor_id, date.today())
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        # 第二步：全部 ready 才处置；有阻塞直接拒绝，不改动任何数据
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                items = conn.execute(
                    "SELECT * FROM disposition_items WHERE batch_id=? ORDER BY id", (batch_id,)
                ).fetchall()
                if not items:
                    raise BusinessError("批次为空，无法执行", 422, "empty_batch")
                states = [{"archive_id": it["archive_id"], "state": it["state"],
                           "blockers": json.loads(it["blockers"])} for it in items]
                blocked = execution_block(states)
                if blocked:
                    raise BusinessError(
                        "存在未核对通过的条目，已退回待确认", 409, "execution_blocked", details=blocked
                    )
                stamp = now()
                for it in items:
                    conn.execute("UPDATE archives SET disposed_at=? WHERE id=?", (stamp, it["archive_id"]))
                    conn.execute(
                        "UPDATE disposition_items SET state='disposed',disposed_at=?,checked_at=? WHERE id=?",
                        (stamp, stamp, it["id"]),
                    )
                    self._audit(conn, it["archive_id"], actor_id, "disposition.execute",
                                {"batch_id": batch_id, "item_id": it["id"],
                                 "retention_snapshot": it["retention_snapshot"], "disposed_at": stamp})
                conn.execute(
                    "UPDATE disposition_batches SET state='executed',executed_at=?,last_checked_at=? WHERE id=?",
                    (stamp, stamp, batch_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.batch_detail(actor_id, batch_id)

    # ------------------------------------------------------------------
    # 查询与序列化
    # ------------------------------------------------------------------
    def _item_payload(self, conn, item: sqlite3.Row) -> dict:
        archive = conn.execute(
            "SELECT id,name,retention_until,disposed_at FROM archives WHERE id=?", (item["archive_id"],)
        ).fetchone()
        today = date.today()
        freeze = self._active_freeze(conn, item["archive_id"])
        return {
            "item_id": item["id"],
            "archive_id": item["archive_id"],
            "archive_name": archive["name"] if archive else f"#{item['archive_id']}",
            "state": item["state"],
            "blockers": json.loads(item["blockers"]),
            "retention_until": archive["retention_until"] if archive else None,
            "retention_snapshot": item["retention_snapshot"],
            "retention_changed": bool(item["retention_snapshot"] and archive
                                      and item["retention_snapshot"] != archive["retention_until"]),
            "days_remaining": (parse_date(archive["retention_until"]) - today).days if archive else None,
            "frozen": freeze is not None,
            "freeze_reason": freeze["reason"] if freeze else None,
            "added_by": item["added_by"],
            "added_at": item["added_at"],
            "checked_at": item["checked_at"],
            "confirmed_at": item["confirmed_at"],
            "disposed_at": item["disposed_at"],
        }

    def batch_detail(self, user_id: str, batch_id: int, conn: sqlite3.Connection | None = None) -> dict:
        def _work(c: sqlite3.Connection) -> dict:
            self._user(c, user_id, {"owner", "archivist", "auditor"})
            batch = c.execute("SELECT * FROM disposition_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise BusinessError("处置批次不存在", 404, "not_found")
            items = [self._item_payload(c, r) for r in c.execute(
                "SELECT * FROM disposition_items WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()]
            groups = {g: [] for g in GROUPS}
            for it in items:
                groups[it["state"]].append(it)
            counts = {g: len(groups[g]) for g in GROUPS}
            return {
                "batch": {
                    "id": batch["id"], "name": batch["name"], "state": batch["state"],
                    "created_by": batch["created_by"], "created_at": batch["created_at"],
                    "last_checked_at": batch["last_checked_at"], "executed_at": batch["executed_at"],
                    "item_count": len(items),
                },
                "groups": groups,
                "counts": counts,
                "blocking_items": [
                    {"item_id": it["item_id"], "archive_id": it["archive_id"],
                     "archive_name": it["archive_name"], "state": it["state"], "blockers": it["blockers"]}
                    for it in items if it["state"] != READY and it["state"] != DISPOSED
                ],
                "items": items,
            }
        if conn is not None:
            return _work(conn)
        with self.connect() as c:
            return _work(c)

    def list_batches(self, user_id: str) -> dict:
        with self.connect() as conn:
            self._user(conn, user_id, {"owner", "archivist", "auditor"})
            batches = conn.execute("SELECT * FROM disposition_batches ORDER BY id DESC").fetchall()
            result = []
            for b in batches:
                rows = conn.execute(
                    "SELECT state,COUNT(*) AS n FROM disposition_items WHERE batch_id=? GROUP BY state",
                    (b["id"],),
                ).fetchall()
                counts = {g: 0 for g in GROUPS}
                for r in rows:
                    counts[r["state"]] = r["n"]
                result.append({
                    "id": b["id"], "name": b["name"], "state": b["state"],
                    "created_by": b["created_by"], "created_at": b["created_at"],
                    "last_checked_at": b["last_checked_at"], "executed_at": b["executed_at"],
                    "counts": counts,
                    "total": sum(counts.values()),
                })
            return {"batches": result}

    def console(self, user_id: str) -> dict:
        """处置台总览：按 待确认/可处置/已冻结/已处置 分组，列出阻塞项与最近核对时间。"""
        with self.connect() as conn:
            user = self._user(conn, user_id, {"owner", "archivist", "auditor"})
            today = date.today()
            rows = conn.execute(
                """SELECT di.*, db.name AS batch_name, db.last_checked_at AS batch_checked_at,
                          db.state AS batch_state
                   FROM disposition_items di
                   JOIN disposition_batches db ON db.id=di.batch_id
                   WHERE db.state='open'
                   ORDER BY CASE di.state WHEN 'frozen' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                            di.checked_at, di.id""",
            ).fetchall()
            groups: dict[str, list] = {g: [] for g in GROUPS}
            for r in rows:
                payload = self._item_payload(conn, r) | {
                    "batch_id": r["batch_id"], "batch_name": r["batch_name"],
                    "batch_last_checked_at": r["batch_checked_at"],
                }
                groups[payload["state"]].append(payload)
            blockers = [
                {"item_id": it["item_id"], "batch_id": it["batch_id"], "archive_id": it["archive_id"],
                 "archive_name": it["archive_name"], "state": it["state"],
                 "blockers": it["blockers"], "freeze_reason": it["freeze_reason"],
                 "last_checked_at": it["checked_at"]}
                for it in groups[PENDING] + groups[FROZEN]
            ]
            executed = conn.execute(
                """SELECT di.*, db.name AS batch_name, db.last_checked_at AS batch_checked_at,
                          db.state AS batch_state
                   FROM disposition_items di
                   JOIN disposition_batches db ON db.id=di.batch_id
                   WHERE di.state='disposed' ORDER BY di.disposed_at DESC, di.id DESC""",
            ).fetchall()
            groups[DISPOSED] = [self._item_payload(conn, r) | {
                "batch_id": r["batch_id"], "batch_name": r["batch_name"],
                "batch_last_checked_at": r["batch_checked_at"]} for r in executed]
            return {
                "today": today.isoformat(),
                "groups": groups,
                "counts": {g: len(groups[g]) for g in GROUPS},
                "blocking_items": blockers,
                "batches": self.list_batches(user_id)["batches"],
            }
