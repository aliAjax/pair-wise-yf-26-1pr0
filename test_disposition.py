import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, PreservationStore
import disposition_rules as rules


class DispositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.repo = self.store.disposition
        self.due_ids = [a["id"] for a in self.repo.list_due("archivist", date.today())]
        self.assertGreaterEqual(len(self.due_ids), 3)

    def tearDown(self):
        self.tmp.cleanup()

    def _due_before(self, name_key: str) -> int:
        for a in self.repo.list_due("archivist", date.today()):
            if name_key in a["name"]:
                return a["id"]
        raise AssertionError("种子到期档案缺失")

    def test_full_flow_review_freeze_block_and_execute(self):
        disputed = self._due_before("人事影像")
        normal = self._due_before("地籍测绘")
        batch = self.repo.create_batch("archivist", "季度到期处置", [disputed, normal], date.today())
        self.assertEqual(batch["counts"]["pending"], 2)
        self.assertEqual(batch["counts"]["ready"], 0)
        self.assertEqual(len(batch["blockers"]), 2)
        self.assertIsNone(batch["last_checked_at"])

        items = {it["archive_id"]: it for it in sum(batch["groups"].values(), [])}
        id_dispute, id_normal = items[disputed]["id"], items[normal]["id"]

        # 审计员冻结有争议档案，组移动到已冻结
        frozen = self.repo.set_freeze("auditor", id_dispute, "权属争议，等待法务结论")
        self.assertEqual(frozen["status"], rules.FROZEN)
        self.assertEqual(frozen["blocker"]["code"], rules.BLOCK_FROZEN)

        # 管理员核对另一项，进入可处置
        checked = self.repo.recheck_item("archivist", id_normal, date.today())
        self.assertEqual(checked["status"], rules.READY)
        self.assertIsNone(checked["blocker"])
        self.assertIsNotNone(checked["last_checked_at"])

        # 存在冻结项时整批执行：冻结项保留，未变化的可处置项照常执行
        result = self.repo.execute_batch("archivist", batch["id"], date.today())
        self.assertIn(normal, result["disposed_archive_ids"])
        self.assertEqual(result["counts"]["disposed"], 1)
        self.assertEqual(result["counts"]["frozen"], 1)

        # 执行后版本、副本、审计记录仍可查看
        status = self.store.archive_status("owner", normal)
        self.assertEqual(status["archive"]["state"], "disposed")
        self.assertGreaterEqual(len(status["versions"]), 1)
        self.assertTrue(any(a["action"] == "disposition.execute" for a in status["audit"]))
        version_id = status["versions"][0]["id"]
        detail = self.store.get_version("auditor", version_id)
        self.assertEqual(len(detail["copies"]), 2)
        # 处置后写入被拒绝
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", version_id, "offline-disk-c")
        self.assertEqual(ctx.exception.code, "archive_disposed")

        # 冻结解除后退回待确认，核对通过才允许执行
        released = self.repo.release_freeze("auditor", id_dispute, "争议已解决")
        self.assertEqual(released["status"], rules.PENDING)
        self.assertEqual(released["blocker"]["code"], rules.BLOCK_FREEZE_RELEASED)
        blocked = self.repo.execute_batch("archivist", batch["id"], date.today())
        self.assertEqual(blocked["disposed_archive_ids"], [])
        self.assertEqual(blocked["counts"]["pending"], 1)
        self.repo.recheck_item("archivist", id_dispute, date.today())
        result2 = self.repo.execute_batch("archivist", batch["id"], date.today())
        self.assertIn(disputed, result2["disposed_archive_ids"])

    def test_gate_detects_retention_change_and_returns_to_pending(self):
        archive_id = self._due_before("旧办公系统")
        batch = self.repo.create_batch("archivist", "", [archive_id], date.today())
        item_id = batch["groups"][rules.PENDING][0]["id"]
        self.repo.recheck_item("archivist", item_id, date.today())
        # 执行前保留期限被延长（有人改了期限）
        with self.store.connect() as conn:
            conn.execute("UPDATE archives SET retention_until=? WHERE id=?",
                         ((date.today() + timedelta(days=180)).isoformat(), archive_id))
        result = self.repo.execute_batch("archivist", batch["id"], date.today())
        self.assertEqual(result["disposed_archive_ids"], [])
        self.assertEqual(result["counts"]["pending"], 1)
        self.assertEqual(result["groups"][rules.PENDING][0]["blocker"]["code"], rules.BLOCK_RETENTION_CHANGED)
        # 重算仍未到期：核对识别出期限变化，退回待确认
        rechecked = self.repo.recheck_item("archivist", item_id, date.today())
        self.assertEqual(rechecked["status"], rules.PENDING)
        self.assertEqual(rechecked["blocker"]["code"], rules.BLOCK_RETENTION_CHANGED)
        # 期限再改回到已到期的新值：必须先人工核对，闸门不会自动接受新基线
        new_expired = (date.today() - timedelta(days=1)).isoformat()
        with self.store.connect() as conn:
            conn.execute("UPDATE archives SET retention_until=? WHERE id=?", (new_expired, archive_id))
        still_blocked = self.repo.execute_batch("archivist", batch["id"], date.today())
        self.assertEqual(still_blocked["disposed_archive_ids"], [])
        # 人工核对接受新基线后，执行前再次变更期限会被闸门退回
        self.repo.recheck_item("archivist", item_id, date.today())
        with self.store.connect() as conn:
            conn.execute("UPDATE archives SET retention_until=? WHERE id=?",
                         ((date.today() - timedelta(days=5)).isoformat(), archive_id))
        gate_result = self.repo.execute_batch("archivist", batch["id"], date.today())
        self.assertEqual(gate_result["disposed_archive_ids"], [])
        self.assertEqual(gate_result["returned_item_ids"], [item_id])
        self.assertEqual(gate_result["groups"][rules.PENDING][0]["blocker"]["code"], rules.BLOCK_RETENTION_CHANGED)
        # 重新核对未变化的新基线 → 执行成功
        self.repo.recheck_item("archivist", item_id, date.today())
        done = self.repo.execute_batch("archivist", batch["id"], date.today())
        self.assertEqual(done["counts"]["disposed"], 1)
        self.assertEqual(done["state"], "executed")

    def test_freeze_during_ready_pushes_back_and_roles_are_enforced(self):
        archive_id = self.due_ids[0]
        batch = self.repo.create_batch("archivist", "", [archive_id], date.today())
        item_id = batch["groups"][rules.PENDING][0]["id"]
        self.repo.recheck_item("archivist", item_id, date.today())
        # 执行前一刻审计员冻结
        self.repo.set_freeze("auditor", item_id, "新发现利用价值")
        blocked = self.repo.execute_batch("archivist", batch["id"], date.today())
        self.assertEqual(blocked["disposed_archive_ids"], [])
        self.assertTrue(blocked["blocked"])
        refreshed = self.repo.get_batch("archivist", batch["id"], date.today())
        self.assertEqual(refreshed["counts"]["frozen"], 1)

        # 权限：只有审计员能冻结/解冻；只有管理员与所有者能建批次/执行
        with self.assertRaises(BusinessError) as ctx:
            self.repo.set_freeze("archivist", item_id, "越权冻结")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.repo.release_freeze("owner", item_id, "")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.repo.create_batch("auditor", "", [self.due_ids[1]], date.today())
        self.assertEqual(ctx.exception.status, 403)
        # 审计员可以查看处置台并做核对
        self.repo.release_freeze("auditor", item_id, "解除")
        self.assertEqual(self.repo.recheck_item("auditor", item_id, date.today())["status"], rules.READY)
        # 未登录/未知用户
        with self.assertRaises(BusinessError):
            self.repo.list_due(None, date.today())
        with self.assertRaises(BusinessError):
            self.repo.list_due("ghost", date.today())

    def test_batch_validation_and_duplicate_guard(self):
        with self.assertRaises(BusinessError) as ctx:
            self.repo.create_batch("archivist", "", [], date.today())
        self.assertEqual(ctx.exception.code, "invalid_archive_ids")
        not_due = self.store.create_archive(
            "owner", "远期档案", (date.today() + timedelta(days=30)).isoformat())
        with self.assertRaises(BusinessError) as ctx:
            self.repo.create_batch("archivist", "", [not_due["id"]], date.today())
        self.assertEqual(ctx.exception.code, "not_expired")
        with self.assertRaises(BusinessError) as ctx:
            self.repo.create_batch("archivist", "", [99999], date.today())
        self.assertEqual(ctx.exception.code, "not_found")

        archive_id = self.due_ids[-1]
        self.repo.create_batch("archivist", "", [archive_id], date.today())
        with self.assertRaises(BusinessError) as ctx:
            self.repo.create_batch("archivist", "", [archive_id], date.today())
        self.assertEqual(ctx.exception.code, "already_in_batch")

        with self.assertRaises(BusinessError) as ctx:
            self.repo.set_freeze("auditor", 99999, "理由充分")
        self.assertEqual(ctx.exception.code, "not_found")


class RulesUnitTests(unittest.TestCase):
    def test_expiry_boundary_and_gate(self):
        today = date(2026, 9, 26)
        self.assertTrue(rules.is_expired("2026-09-25", today))
        self.assertFalse(rules.is_expired("2026-09-26", today))  # 到期当天仍在保留期
        self.assertFalse(rules.is_expired("2026-10-01", today))
        facts = rules.Facts(rules.READY, "2026-01-01", "2026-01-01", False)
        d = rules.gate(facts, today)
        self.assertEqual((d.status, d.blocker), (rules.READY, None))
        facts_changed = rules.Facts(rules.READY, "2026-01-01", "2027-01-01", False)
        d = rules.gate(facts_changed, today)
        self.assertEqual(d.status, rules.PENDING)
        self.assertEqual(d.blocker["code"], rules.BLOCK_RETENTION_CHANGED)
        facts_frozen = rules.Facts(rules.READY, "2026-01-01", "2026-01-01", True, "争议")
        self.assertEqual(rules.gate(facts_frozen, today).status, rules.FROZEN)


if __name__ == "__main__":
    unittest.main()
