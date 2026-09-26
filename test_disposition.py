"""到期处置台测试：纯规则状态机 + 存储层完整流程 + 角色权限。"""
import base64
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from errors import BusinessError
from rules import (
    BLOCKER_FROZEN, BLOCKER_RETENTION_CHANGED, BLOCKER_RETENTION_NOT_DUE, DISPOSED, FROZEN,
    PENDING, READY, evaluate_blockers, execution_block, is_due, recheck_state,
    state_on_enqueue, state_on_unfreeze,
)
from store import PreservationStore


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def expired_date(days: int = 30) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def future_date(days: int = 400) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


class RuleTests(unittest.TestCase):
    def test_due_and_blockers(self):
        today = date.today()
        self.assertTrue(is_due(expired_date(), today))
        self.assertFalse(is_due(future_date(), today))
        self.assertEqual(
            evaluate_blockers(frozen=False, due=True, retention_changed=False, already_disposed=False), []
        )
        self.assertIn(BLOCKER_RETENTION_NOT_DUE,
                      evaluate_blockers(frozen=False, due=False, retention_changed=False, already_disposed=False))
        self.assertIn(BLOCKER_FROZEN,
                      evaluate_blockers(frozen=True, due=True, retention_changed=False, already_disposed=False))
        self.assertIn(BLOCKER_RETENTION_CHANGED,
                      evaluate_blockers(frozen=False, due=True, retention_changed=True, already_disposed=False))
        # 未确认过的条目，期限变化不产生额外阻塞
        self.assertNotIn(BLOCKER_RETENTION_CHANGED,
                         evaluate_blockers(frozen=False, due=False, retention_changed=True,
                                           already_disposed=False, confirmed=False))

    def test_state_machine(self):
        self.assertEqual(state_on_enqueue(frozen=False, due=True), READY)
        self.assertEqual(state_on_enqueue(frozen=True, due=True), FROZEN)
        self.assertEqual(state_on_enqueue(frozen=False, due=False), PENDING)
        # 执行前核对：出现冻结/期限变化 -> 退回
        self.assertEqual(recheck_state(state=READY, blockers=[BLOCKER_FROZEN]), PENDING)
        self.assertEqual(recheck_state(state=READY, blockers=[]), READY)
        # 解冻不自动恢复可处置；已确认条目的期限快照若已变则停留在待确认
        self.assertEqual(state_on_unfreeze(state=FROZEN, due=True, retention_changed=True, confirmed=True), PENDING)
        self.assertEqual(state_on_unfreeze(state=FROZEN, due=True, retention_changed=False, confirmed=True), READY)
        # 从未确认过的条目即使快照不一致，只要当前到期且无冻结，解冻后即可处置
        self.assertEqual(state_on_unfreeze(state=FROZEN, due=True, retention_changed=True, confirmed=False), READY)
        # 执行闸
        self.assertEqual(execution_block([{"archive_id": 1, "state": READY, "blockers": []}]), [])
        blocked = execution_block([
            {"archive_id": 1, "state": READY, "blockers": []},
            {"archive_id": 2, "state": PENDING, "blockers": [BLOCKER_RETENTION_CHANGED]},
        ])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["archive_id"], 2)


class DispositionFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        # seed 已含两个到期档案；另造一个独立到期档案便于精确控制
        self.due_archive = self._create_archive("项目A-到期合同", expired_date(10))
        self.other_due = self._create_archive("项目B-到期账册", expired_date(60))
        self.locked_archive = self._create_archive("项目C-争议图纸", expired_date(5))

    def tearDown(self):
        self.tmp.cleanup()

    def _create_archive(self, name: str, retention: str) -> int:
        # create_archive 不允许过去日期，直接构造数据以模拟"已经到期"的历史档案
        with self.store.connect() as conn:
            cur = conn.execute(
                "INSERT INTO archives(name,owner_id,retention_until,restricted,created_at) VALUES(?,?,?,1,?)",
                (name, "owner", retention, "2020-01-01T00:00:00+00:00"),
            )
            aid = cur.lastrowid
            conn.execute(
                "INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,'write')", (aid, "archivist")
            )
            conn.execute(
                "INSERT INTO archive_members(archive_id,user_id,permission) VALUES(?,?,'read')", (aid, "auditor")
            )
            conn.execute(
                "INSERT INTO archive_versions(archive_id,version,created_by,created_at) VALUES(?,1,'archivist','2020-01-01T00:00:00+00:00')",
                (aid,),
            )
            vid = conn.execute("SELECT id FROM archive_versions WHERE archive_id=?", (aid,)).fetchone()[0]
            conn.execute(
                "INSERT INTO archive_files(version_id,path,sha256,size,content) VALUES(?,?,?,?,?)",
                (vid, "a.txt", "h", 1, b"x"),
            )
            copy = conn.execute(
                "INSERT INTO copies(version_id,location,created_at,last_verified_at) VALUES(?, 'disk-a', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')",
                (vid,),
            ).lastrowid
            conn.execute(
                "INSERT INTO copy_files(copy_id,path,sha256,size,content) VALUES(?,?,?,?,?)",
                (copy, "a.txt", "h", 1, b"x"),
            )
        return aid

    def test_batch_execute_happy_path_and_readonly_after(self):
        batch = self.store.create_batch("archivist", "2026Q3 到期批次",
                                        [self.due_archive, self.other_due])
        bid = batch["batch"]["id"]
        self.assertEqual(batch["counts"][READY], 2)
        self.assertIsNotNone(batch["batch"]["last_checked_at"])
        # 执行前再核对 -> 无变化 -> 执行成功
        checked = self.store.check_batch("archivist", bid)
        self.assertEqual(checked["changed"], [])
        result = self.store.execute_batch("archivist", bid)
        self.assertEqual(result["batch"]["state"], "executed")
        self.assertTrue(all(it["state"] == DISPOSED for it in result["items"]))
        # 执行后版本、副本、审计记录仍可查看
        status = self.store.archive_status("auditor", self.due_archive)
        self.assertTrue(status["archive"]["disposed_at"])
        self.assertEqual(len(status["versions"]), 1)
        self.assertIn("disposition.execute", [a["action"] for a in status["audit"]])
        # 处置后写入被拒绝
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("archivist", self.due_archive,
                                      [{"path": "new.txt", "content_b64": b64(b"n")}])
        self.assertEqual(ctx.exception.code, "archive_disposed")
        # 已执行批次不可再核对/追加/执行
        with self.assertRaises(BusinessError) as ctx:
            self.store.check_batch("archivist", bid)
        self.assertEqual(ctx.exception.code, "batch_executed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_archives("archivist", bid, [self.other_due])
        self.assertEqual(ctx.exception.code, "batch_executed")

    def test_freeze_blocks_execution_and_auditor_only(self):
        batch = self.store.create_batch("archivist", "含争议批次", [self.due_archive, self.locked_archive])
        bid = batch["batch"]["id"]
        items = {it["archive_id"]: it["item_id"] for it in batch["items"]}
        # 只有 auditor 能冻结
        with self.assertRaises(BusinessError) as ctx:
            self.store.freeze_item("archivist", bid, items[self.locked_archive], "诉讼争议")
        self.assertEqual(ctx.exception.status, 403)
        # outsider 不是档案成员，也不能冻结
        with self.assertRaises(BusinessError) as ctx:
            self.store.freeze_item("outsider", bid, items[self.locked_archive], "诉讼争议")
        self.assertEqual(ctx.exception.status, 403)
        frozen = self.store.freeze_item("auditor", bid, items[self.locked_archive], "诉讼争议，待法务确认")
        self.assertEqual(frozen["counts"][FROZEN], 1)
        item = next(i for i in frozen["items"] if i["archive_id"] == self.locked_archive)
        self.assertEqual(item["state"], FROZEN)
        self.assertIn(BLOCKER_FROZEN, item["blockers"])
        # 执行被拦，阻塞项随 409 返回
        with self.assertRaises(BusinessError) as ctx:
            self.store.execute_batch("archivist", bid)
        self.assertEqual(ctx.exception.code, "execution_blocked")
        self.assertEqual([d["archive_id"] for d in ctx.exception.details], [self.locked_archive])
        # 处置台分组与阻塞清单
        console = self.store.console("archivist")
        self.assertEqual(len(console["groups"][FROZEN]), 1)
        self.assertEqual(console["groups"][READY][0]["archive_id"], self.due_archive)
        blockers = {(b["archive_id"], b["state"]) for b in console["blocking_items"]}
        self.assertIn((self.locked_archive, FROZEN), blockers)
        # 解冻且事实无变化 -> 可处置，执行成功
        self.store.unfreeze_item("auditor", bid, items[self.locked_archive])
        result = self.store.execute_batch("archivist", bid)
        self.assertTrue(all(it["state"] == DISPOSED for it in result["items"]))

    def test_retention_extension_returns_items_to_pending(self):
        batch = self.store.create_batch("archivist", "期限变动批次", [self.due_archive])
        bid = batch["batch"]["id"]
        item_id = batch["items"][0]["item_id"]
        # 管理员把保留期限延后
        self.store.update_retention("owner", self.due_archive, future_date(200))
        detail = self.store.batch_detail("archivist", bid)
        self.assertEqual(detail["groups"][READY], [])
        self.assertEqual(detail["groups"][PENDING][0]["item_id"], item_id)
        # 尝试确认仍被拒（期限未到 + 快照不一致）
        resp = self.store.confirm_items("archivist", bid, [item_id])
        self.assertEqual(resp["groups"][PENDING][0]["item_id"], item_id)
        rejected_codes = {c for r in resp["rejected"] for c in r["blockers"]}
        self.assertIn(BLOCKER_RETENTION_NOT_DUE, rejected_codes)
        # 执行被闸住
        with self.assertRaises(BusinessError) as ctx:
            self.store.execute_batch("archivist", bid)
        self.assertEqual(ctx.exception.code, "execution_blocked")

    def test_not_due_archive_starts_pending_and_duplicate_guard(self):
        fresh = self.store.create_archive("owner", "新建长期档案", future_date(500))["id"]
        batch = self.store.create_batch("archivist", "混合批次", [self.due_archive, fresh])
        bid = batch["batch"]["id"]
        self.assertEqual(batch["counts"][PENDING], 1)
        self.assertEqual(batch["counts"][READY], 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_archives("archivist", bid, [self.due_archive])
        self.assertEqual(ctx.exception.code, "already_in_batch")
        # 候选清单排除已在打开批次中的档案
        candidates = {c["archive_id"] for c in self.store.list_due_candidates("archivist")["candidates"]}
        self.assertNotIn(self.due_archive, candidates)
        self.assertIn(self.other_due, candidates)
        # 未到期（从未确认）条目即使期限再延长，阻塞项也只有"未到期"，不含 retention_changed
        self.store.update_retention("owner", fresh, future_date(900))
        detail = self.store.batch_detail("archivist", bid)
        pending = next(g for g in detail["groups"][PENDING] if g["archive_id"] == fresh)
        self.assertEqual(pending["blockers"], [BLOCKER_RETENTION_NOT_DUE])

    def test_check_picks_up_new_freeze_and_auditor_can_view(self):
        batch = self.store.create_batch("archivist", "核对批次", [self.due_archive])
        bid = batch["batch"]["id"]
        item_id = batch["items"][0]["item_id"]
        # 绕过批次直接加冻结（模拟其他争议入口），核对应发现
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO freezes(archive_id,reason,active,created_by,created_at) VALUES(?, ?,1,'auditor',?)",
                (self.due_archive, "外部协查", "2026-09-26T00:00:00+00:00"),
            )
        checked = self.store.check_batch("auditor", bid)
        changed = {c["item_id"]: c["state"] for c in checked["changed"]}
        self.assertEqual(changed[item_id], FROZEN)


if __name__ == "__main__":
    unittest.main()
