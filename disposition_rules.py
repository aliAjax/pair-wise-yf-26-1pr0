"""到期处置台业务规则（纯函数，不访问数据库或网络，便于单独测试）。

处置项生命周期：
    pending（待确认）──人工核对通过──▶ ready（可处置）──执行──▶ disposed（已处置）
       ▲              │                  │
       │              ▼                  ▼
       └──────── frozen（已冻结，审计保全）/ 执行前发现变化退回

关键区别：
- review（人工核对）：当前仍到期且未冻结时，以当前期限为新基线，转可处置；
- gate（执行前闸门）：严格对照已确认基线，期限或冻结状态一旦变化一律退回，
  绝不自动接受新期限。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# 处置项状态
PENDING = "pending"      # 待确认
READY = "ready"          # 可处置
FROZEN = "frozen"        # 已冻结
DISPOSED = "disposed"    # 已处置
ACTIVE_STATES = (PENDING, READY, FROZEN)

# 阻塞原因代码
BLOCK_AWAITING_REVIEW = "awaiting_review"     # 刚纳入批次，尚未核对
BLOCK_FROZEN = "frozen"                        # 审计员保全冻结
BLOCK_NOT_EXPIRED = "not_expired"              # 期限未到（含延期后）
BLOCK_RETENTION_CHANGED = "retention_changed"  # 执行前发现期限被改动
BLOCK_FREEZE_RELEASED = "freeze_released"      # 冻结解除，需重新核对


@dataclass(frozen=True)
class Facts:
    """规则判断所需的事实快照，由数据层装配。"""
    status: str
    retention_snapshot: str   # 上次确认时的保留期限（基线）
    current_retention: str    # 档案当前保留期限
    frozen: bool
    freeze_reason: str | None = None
    frozen_by: str | None = None


@dataclass(frozen=True)
class Decision:
    status: str
    blocker: dict | None      # {"code": ..., "message": ...}
    snapshot: str | None      # 需要持久化的新基线期限
    changed: bool


def is_expired(retention_until: str, today: date) -> bool:
    """到期定义：保留期限早于今天（到期日当天仍在保留期内）。"""
    return date.fromisoformat(retention_until) < today


def new_item_blocker() -> dict:
    return {"code": BLOCK_AWAITING_REVIEW, "message": "刚纳入处置批次，等待核对期限与冻结状态"}


def _freeze_blocker(facts: Facts) -> dict:
    tail = f"：{facts.freeze_reason}" if facts.freeze_reason else ""
    who = f"（{facts.frozen_by}）" if facts.frozen_by else ""
    return {"code": BLOCK_FROZEN, "message": f"审计保全冻结中{tail}{who}"}


def review(facts: Facts, today: date) -> Decision:
    """人工核对确认：期限仍有效则接受现状为新基线。"""
    if facts.frozen:
        return Decision(FROZEN, _freeze_blocker(facts), None, facts.status != FROZEN)
    if not is_expired(facts.current_retention, today):
        return Decision(
            PENDING,
            {"code": BLOCK_NOT_EXPIRED,
             "message": f"档案尚未到期（保留至 {facts.current_retention}），暂不能处置"},
            None,
            facts.status != PENDING,
        )
    return Decision(READY, None, facts.current_retention, facts.status != READY)


def gate(facts: Facts, today: date) -> Decision:
    """批次开始执行前的最后核对：只与已确认基线做严格比对。"""
    if facts.frozen:
        return Decision(FROZEN, _freeze_blocker(facts), None, facts.status != FROZEN)
    if facts.current_retention != facts.retention_snapshot:
        return Decision(
            PENDING,
            {"code": BLOCK_RETENTION_CHANGED,
             "message": f"保留期限已由 {facts.retention_snapshot} 变更为 {facts.current_retention}，退回待确认"},
            None,
            True,
        )
    if not is_expired(facts.current_retention, today):
        return Decision(
            PENDING,
            {"code": BLOCK_NOT_EXPIRED,
             "message": f"档案尚未到期（保留至 {facts.current_retention}），暂不能处置"},
            None,
            facts.status != PENDING,
        )
    return Decision(READY, None, None, False)


def released_blocker() -> dict:
    return {"code": BLOCK_FREEZE_RELEASED, "message": "保全冻结已解除，需重新核对后才能处置"}


def group_items(items: list[dict]) -> dict[str, list[dict]]:
    """处置台分组：待确认 / 可处置 / 已冻结 / 已处置。"""
    groups: dict[str, list[dict]] = {PENDING: [], READY: [], FROZEN: [], DISPOSED: []}
    for item in items:
        groups[item["status"]].append(item)
    return groups
