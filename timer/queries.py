"""GPU 时间戳查询的环形槽池。

tick 契约
=========
所有 ``tick`` 都是整数 GPU 时间戳计数，1 tick 就是 1 个时间戳单位；
本模块不做秒/毫秒换算，也不读系统时钟，所有时间都由调用方显式传入。
回读边界保留 1 tick 容差：``end_tick <= available_until + 1`` 即视为
该查询已可回读。

池结构
======
``pool`` 是普通 dict，不使用自定义类::

    {
        "capacity": 8,            # 环形池大小，槽数必须等于它
        "cursor": 0,              # 下一次环形扫描的起点 0..capacity-1
        "slots": [                # 长度必须 == capacity
            {
                "state": "free",  # "free" | "inflight" | "resolved"
                "begin_tick": None,
                "end_tick": None,
                "result": None,   # resolve 成功后保存回读结果
            },
            ...
        ],
    }

槽索引始终位于 ``0 .. capacity-1``；环形扫描按下标对 capacity 取模推进，
绝不越界。槽状态只有 free / inflight / resolved 三种，因此
``stats()`` 的三个计数之和恒等于 ``capacity``。

查询结果
========
一条结果固定为::

    {"slot": int, "begin_tick": int, "end_tick": int, "duration": int}

其中 ``duration = end_tick - begin_tick``，单位仍是 tick。
"""

from __future__ import annotations

_STATES = ("free", "inflight", "resolved")
_SLOT_KEYS = ("state", "begin_tick", "end_tick", "result")


class TimerError(Exception):
    """计时池的非法参数或非法状态。"""


def _is_int(value) -> bool:
    """严格判断整数，排除 bool（bool 是 int 的子类）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_tick(value, name):
    if not _is_int(value):
        raise TimerError(f"{name} 必须是整数 tick，收到 {value!r}")
    return value


def _validate_slot(slot, index):
    if not isinstance(slot, dict):
        raise TimerError(f"槽 {index} 不是 dict")
    for key in _SLOT_KEYS:
        if key not in slot:
            raise TimerError(f"槽 {index} 缺少字段 {key!r}")
    state = slot["state"]
    if state not in _STATES:
        raise TimerError(f"槽 {index} 状态非法：{state!r}")
    for key in ("begin_tick", "end_tick"):
        value = slot[key]
        if value is not None and not _is_int(value):
            raise TimerError(f"槽 {index} 的 {key} 必须是整数或 None")
    if state == "free" and (
        slot["begin_tick"] is not None or slot["end_tick"] is not None
    ):
        raise TimerError(f"槽 {index} 为 free 但仍有 tick 数据")


def _validate_pool(pool) -> int:
    """校验池结构，返回 capacity。"""
    if not isinstance(pool, dict):
        raise TimerError("pool 必须是 dict")
    capacity = pool.get("capacity")
    if not _is_int(capacity) or capacity <= 0:
        raise TimerError(f"capacity 必须是正整数，收到 {capacity!r}")
    slots = pool.get("slots")
    if not isinstance(slots, (list, tuple)) or len(slots) != capacity:
        raise TimerError("slots 长度必须等于 capacity")
    cursor = pool.get("cursor")
    if not _is_int(cursor) or not (0 <= cursor < capacity):
        raise TimerError(f"cursor 必须位于 0..{capacity - 1}，收到 {cursor!r}")
    for index, slot in enumerate(slots):
        _validate_slot(slot, index)
    return capacity


def _check_slot_index(pool, slot, capacity) -> int:
    if not _is_int(slot):
        raise TimerError(f"槽下标必须是整数，收到 {slot!r}")
    if not (0 <= slot < capacity):
        raise TimerError(f"槽下标 {slot} 越界（capacity={capacity}）")
    return slot


def _slot_result(slot_index, slot) -> dict:
    begin_tick = slot["begin_tick"]
    end_tick = slot["end_tick"]
    return {
        "slot": slot_index,
        "begin_tick": begin_tick,
        "end_tick": end_tick,
        "duration": end_tick - begin_tick,
    }


def alloc_slot(pool) -> int:
    """从环形池分配一个空闲槽，返回槽下标。

    契约：

    - ``capacity <= 0``、``slots`` 长度与 ``capacity`` 不一致、槽结构
      非法时抛 :class:`TimerError`；
    - 从 ``pool["cursor"]`` 开始环形扫描（下标对 ``capacity`` 取模），
      取第一个 ``state == "free"`` 的槽，把它置为 ``"inflight"`` 并清空
      ``begin_tick`` / ``end_tick`` / ``result``，游标推进到下一格，
      返回该槽下标；
    - 池中没有空闲槽（空池）时返回 ``-1``；绝不覆盖 inflight 或
      resolved 的槽，也绝不返回 ``0`` 来冒充失败。
    """
    capacity = _validate_pool(pool)
    cursor = pool["cursor"]
    slots = pool["slots"]

    for offset in range(capacity):
        index = (cursor + offset) % capacity
        slot = slots[index]
        if slot["state"] != "free":
            continue
        slot["state"] = "inflight"
        slot["begin_tick"] = None
        slot["end_tick"] = None
        slot["result"] = None
        pool["cursor"] = (index + 1) % capacity
        return index

    return -1


def begin(pool, slot, tick) -> dict:
    """在已分配的槽上记录开始 tick，返回该槽快照。

    契约：

    - ``slot`` 必须是 :func:`alloc_slot` 刚分配、尚未 begin 的 inflight
      槽；重复 begin（同一槽第二次调用）、槽越界、槽不是 inflight 或
      已有 ``begin_tick`` 时抛 :class:`TimerError`；
    - 成功时写入 ``begin_tick = tick``、``end_tick = None``，返回
      ``{"slot": int, "state": "inflight", "begin_tick": int,
      "end_tick": None}``；
    - ``tick`` 单位与池一致，不进行任何换算，也不改写其他槽。
    """
    capacity = _validate_pool(pool)
    slot_index = _check_slot_index(pool, slot, capacity)
    _require_tick(tick, "tick")

    target = pool["slots"][slot_index]
    if target["state"] != "inflight":
        raise TimerError(
            f"槽 {slot_index} 状态为 {target['state']!r}，不能 begin"
        )
    if target["begin_tick"] is not None:
        raise TimerError(f"槽 {slot_index} 已经 begin，不能重复 begin")

    target["begin_tick"] = tick
    target["end_tick"] = None
    return {
        "slot": slot_index,
        "state": "inflight",
        "begin_tick": tick,
        "end_tick": None,
    }


def end(pool, slot, tick) -> dict:
    """在已 begin 的槽上记录结束 tick，返回耗时快照。

    契约：

    - 槽必须已 begin 且尚未 end，否则抛 :class:`TimerError`；
    - ``tick < begin_tick`` 表示时间戳回退，必须抛 :class:`TimerError`，
      不允许产生负耗时；
    - 成功时写入 ``end_tick = tick``，返回
      ``{"slot": int, "begin_tick": int, "end_tick": int,
      "duration": int}``，其中 ``duration = tick - begin_tick``，单位 tick。
    """
    capacity = _validate_pool(pool)
    slot_index = _check_slot_index(pool, slot, capacity)
    _require_tick(tick, "tick")

    target = pool["slots"][slot_index]
    if target["state"] != "inflight" or target["begin_tick"] is None:
        raise TimerError(f"槽 {slot_index} 尚未 begin，不能 end")
    if target["end_tick"] is not None:
        raise TimerError(f"槽 {slot_index} 已经 end，不能重复 end")
    if tick < target["begin_tick"]:
        raise TimerError(
            f"tick 回退：end={tick} < begin={target['begin_tick']}"
        )

    target["end_tick"] = tick
    return _slot_result(slot_index, target)


def resolve(pool, available_until) -> list[dict]:
    """回读已结束的查询，返回按 tick 升序排列的结果列表。

    契约：

    - 只回读 ``end_tick is not None`` 且 ``end_tick <= available_until + 1``
      （1 tick 容差）的查询；未完成（还没 end）的查询绝不返回，也绝不
      把它的耗时读成 0；
    - 每条结果形如 ``{"slot", "begin_tick", "end_tick", "duration"}``；
    - 返回列表按 ``begin_tick`` 升序排列，同一 begin tick 按槽下标升序；
    - 被回读的槽置为 ``"resolved"`` 并保存结果；不可回读的槽状态不变。
    """
    _validate_pool(pool)
    _require_tick(available_until, "available_until")
    limit = available_until + 1

    results = []
    for slot_index, slot in enumerate(pool["slots"]):
        if slot["state"] == "resolved":
            continue
        if slot["end_tick"] is None:
            continue
        if slot["end_tick"] > limit:
            continue
        result = _slot_result(slot_index, slot)
        slot["state"] = "resolved"
        slot["result"] = dict(result)
        results.append(result)

    results.sort(key=lambda item: (item["begin_tick"], item["slot"]))
    return results


def aggregate(results, buckets) -> dict:
    """按耗时桶统计 min / max / avg。

    契约：

    - ``buckets`` 是 ``[(lo, hi), ...]`` 形式的 tick 闭区间列表；
    - 返回 ``{桶下标: {"min": int, "max": int, "avg": float,
      "count": int}}``，键顺序与 ``buckets`` 一致；
    - 只统计已结束（``end_tick is not None``）的结果，未完成查询不得
      进入平均值；
    - 结果按顺序落入第一个匹配的桶，空桶的 min / max / avg 均为 ``0``。
    """
    if not isinstance(buckets, (list, tuple)):
        raise TimerError("buckets 必须是 (lo, hi) 列表")

    normalized = []
    for bucket in buckets:
        if not isinstance(bucket, (list, tuple)) or len(bucket) != 2:
            raise TimerError(f"非法桶定义：{bucket!r}")
        lo, hi = bucket
        if not _is_int(lo) or not _is_int(hi) or lo > hi:
            raise TimerError(f"非法桶区间：{bucket!r}")
        normalized.append((lo, hi))

    stats_by_bucket = [
        {"min": 0, "max": 0, "avg": 0.0, "count": 0, "_sum": 0}
        for _ in normalized
    ]

    if results is None:
        results = []
    if not isinstance(results, (list, tuple)):
        raise TimerError("results 必须是结果列表")

    for result in results:
        if not isinstance(result, dict):
            raise TimerError("结果必须是 dict")
        begin_tick = result.get("begin_tick")
        end_tick = result.get("end_tick")
        if end_tick is None:
            # 未完成查询不参与聚合。
            continue
        if not _is_int(begin_tick) or not _is_int(end_tick):
            raise TimerError(f"结果 tick 非法：{result!r}")
        duration = end_tick - begin_tick
        if duration < 0:
            raise TimerError(f"结果耗时为负：{result!r}")
        for bucket_index, (lo, hi) in enumerate(normalized):
            if lo <= duration <= hi:
                bucket_stats = stats_by_bucket[bucket_index]
                if bucket_stats["count"] == 0:
                    bucket_stats["min"] = duration
                    bucket_stats["max"] = duration
                else:
                    bucket_stats["min"] = min(bucket_stats["min"], duration)
                    bucket_stats["max"] = max(bucket_stats["max"], duration)
                bucket_stats["count"] += 1
                bucket_stats["_sum"] += duration
                break

    output = {}
    for bucket_index, bucket_stats in enumerate(stats_by_bucket):
        count = bucket_stats["count"]
        output[bucket_index] = {
            "min": bucket_stats["min"],
            "max": bucket_stats["max"],
            "avg": (bucket_stats["_sum"] / count) if count else 0.0,
            "count": count,
        }
    return output


def reuse(pool, slot) -> dict:
    """释放一个已回读的槽，返回 ``{"slot": int, "state": "free"}``。

    契约：

    - 只有 ``state == "resolved"`` 的槽可以复用；未 resolve 的槽
      （inflight，包括已 end 但尚未回读）必须抛 :class:`TimerError`，
      以免丢掉尚未回读的结果；
    - 复用后清空 ``begin_tick`` / ``end_tick`` / ``result``，槽回到
      ``"free"``，可再次被 :func:`alloc_slot` 分配；
    - 槽越界或结构非法时抛 :class:`TimerError`。
    """
    capacity = _validate_pool(pool)
    slot_index = _check_slot_index(pool, slot, capacity)

    target = pool["slots"][slot_index]
    if target["state"] != "resolved":
        raise TimerError(
            f"槽 {slot_index} 状态为 {target['state']!r}，只有 resolved "
            "的槽可以复用"
        )

    target["state"] = "free"
    target["begin_tick"] = None
    target["end_tick"] = None
    target["result"] = None
    return {"slot": slot_index, "state": "free"}


def stats(pool) -> dict:
    """返回 ``{"inflight": int, "free": int, "resolved": int}``。

    契约：

    - 三个计数按槽状态划分并覆盖全部 ``capacity`` 个槽，三者之和恒等于
      ``capacity``，不允许出现空洞或重复计数；
    - 已 end 但尚未回读的槽仍算 ``inflight``（占着槽，但结果还没取走）；
    - ``capacity <= 0`` 或池结构非法时抛 :class:`TimerError`。
    """
    _validate_pool(pool)
    counts = {"inflight": 0, "free": 0, "resolved": 0}
    for slot in pool["slots"]:
        counts[slot["state"]] += 1
    return counts


def query_batch(pools, available_until) -> dict:
    """批量回读多个池，返回 ``{池名: [结果, ...]}``。

    契约：

    - ``pools`` 是 ``{池名: pool}``，返回 dict 的键顺序与输入一致
      （批量全成功时顺序不变）；
    - 先校验全部池（结构、capacity、slots 长度、槽状态），任一池非法
      立即抛 :class:`TimerError`；此时所有池必须保持调用前状态（整体
      回滚，不能出现半提交）；
    - 全部合法时逐池执行 :func:`resolve` 语义，池内结果按 tick 升序；
    - 各池必须使用相互独立的结果列表，不能共用缓冲导致池之间串数据。
    """
    if not isinstance(pools, dict):
        raise TimerError("pools 必须是 {池名: pool} 的 dict")
    _require_tick(available_until, "available_until")

    # 阶段一：整体校验。任何一池非法都在改动任何状态之前抛出。
    for name, pool in pools.items():
        try:
            _validate_pool(pool)
        except TimerError as exc:
            raise TimerError(f"池 {name!r} 非法：{exc}") from exc

    # 快照全部槽，便于执行阶段出现意外时整体回滚（原地恢复，保持引用）。
    snapshots = {
        name: ([dict(slot) for slot in pool["slots"]], pool["cursor"])
        for name, pool in pools.items()
    }

    output = {}
    try:
        for name, pool in pools.items():
            # resolve 每次返回全新列表，池与池之间不共享缓冲。
            output[name] = resolve(pool, available_until)
    except Exception:
        for name, pool in pools.items():
            slot_snapshots, cursor = snapshots[name]
            for slot, snapshot in zip(pool["slots"], slot_snapshots):
                slot.clear()
                slot.update(snapshot)
            pool["cursor"] = cursor
        raise

    return output
