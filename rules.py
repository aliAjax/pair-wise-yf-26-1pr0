"""到期处置的纯业务规则：状态判定、阻塞项、状态机。

本模块不碰数据库和 HTTP，全部为纯函数，便于单元测试。
批次条目状态机：

    纳入时未到期/被冻结  -> pending（待确认）
    核对后可处置         -> ready（可处置）
    审计员冻结           -> frozen（已冻结）
    执行完成             -> disposed（已处置）

规则要点：执行前必须重新核对保留期限与冻结状态；
只要期限快照与当前值不一致或出现冻结，一律退回 pending/frozen。
"""
from __future__ import annotations

from datetime import date

# 处置批次条目状态
PENDING = "pending"      # 待确认：存在阻塞项，或管理员尚未确认
READY = "ready"          # 可处置：已核对且无阻塞
FROZEN = "frozen"        # 已冻结：审计员发起保全冻结
DISPOSED = "disposed"    # 已处置：已执行到期处置

OPEN_STATES = (PENDING, READY, FROZEN)
ALL_STATES = (PENDING, READY, FROZEN, DISPOSED)

# 批次状态
BATCH_OPEN = "open"
BATCH_EXECUTED = "executed"

# 阻塞项代码 -> 说明
BLOCKER_RETENTION_NOT_DUE = "retention_not_due"      # 尚未到保留期限
BLOCKER_RETENTION_CHANGED = "retention_changed"      # 期限在确认后被改动
BLOCKER_FROZEN = "frozen"                            # 审计保全冻结中
BLOCKER_DISPOSED = "already_disposed"                # 档案已在其他批次处置

BLOCKER_MESSAGES = {
    BLOCKER_RETENTION_NOT_DUE: "保留期限未到，暂不能处置",
    BLOCKER_RETENTION_CHANGED: "保留期限在确认后发生变化，需要重新确认",
    BLOCKER_FROZEN: "审计员已对该档案做保全冻结",
    BLOCKER_DISPOSED: "档案已完成处置",
}

# 处置台分组顺序（已处置单独成组）
GROUPS = (PENDING, READY, FROZEN, DISPOSED)
GROUP_LABELS = {
    PENDING: "待确认",
    READY: "可处置",
    FROZEN: "已冻结",
    DISPOSED: "已处置",
}


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError("日期必须是 YYYY-MM-DD")


def days_remaining(retention_until: str, today: date) -> int:
    """剩余天数；负数表示已过期。"""
    return (parse_date(retention_until) - today).days


def is_due(retention_until: str, today: date) -> bool:
    return days_remaining(retention_until, today) <= 0


def evaluate_blockers(*, frozen: bool, due: bool, retention_changed: bool,
                      already_disposed: bool, confirmed: bool = True) -> list[str]:
    """根据当前事实计算阻塞项。已处置优先级最高，其次冻结。

    retention_changed 只在条目已确认（有期限快照且管理员确认过）时才算阻塞：
    从未确认过的待确认条目本就等待确认，期限再变化不产生额外阻塞。
    """
    blockers: list[str] = []
    if already_disposed:
        blockers.append(BLOCKER_DISPOSED)
    if frozen:
        blockers.append(BLOCKER_FROZEN)
    if confirmed and retention_changed:
        blockers.append(BLOCKER_RETENTION_CHANGED)
    if not due:
        blockers.append(BLOCKER_RETENTION_NOT_DUE)
    return blockers


def state_on_enqueue(*, frozen: bool, due: bool) -> str:
    """纳入批次时的初始状态：到期且未冻结即为可处置，否则退回待确认。"""
    if frozen:
        return FROZEN
    return READY if due else PENDING


def state_on_freeze(state: str) -> str:
    if state == DISPOSED:
        return state
    return FROZEN


def state_on_unfreeze(*, state: str, due: bool, retention_changed: bool, confirmed: bool) -> str:
    """解冻后的状态由当时事实重新决定，不自动恢复为可处置。"""
    if state != FROZEN:
        return state
    blockers = evaluate_blockers(
        frozen=False, due=due, retention_changed=retention_changed,
        already_disposed=False, confirmed=confirmed,
    )
    return READY if not blockers else PENDING


def recheck_state(*, state: str, blockers: list[str]) -> str:
    """执行前核对：已冻结/已处置保持；其余有阻塞退回待确认，无阻塞才可处置。"""
    if state in (FROZEN, DISPOSED):
        return state
    return PENDING if blockers else READY


def can_confirm(blockers: list[str]) -> bool:
    return not blockers


def execution_block(states: list[str]) -> list[dict]:
    """批次执行闸：只允许全部条目处于 ready。返回阻塞明细。"""
    blocked = []
    for s in states:
        if s["state"] != READY:
            blocked.append({
                "archive_id": s["archive_id"],
                "state": s["state"],
                "blockers": s.get("blockers", []),
            })
    return blocked
