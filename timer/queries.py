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


class TimerError(Exception):
    """计时池的非法参数或非法状态。"""


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


def stats(pool) -> dict:
    """返回 ``{"inflight": int, "free": int, "resolved": int}``。

    契约：

    - 三个计数按槽状态划分并覆盖全部 ``capacity`` 个槽，三者之和恒等于
      ``capacity``，不允许出现空洞或重复计数；
    - 已 end 但尚未回读的槽仍算 ``inflight``（占着槽，但结果还没取走）；
    - ``capacity <= 0`` 或池结构非法时抛 :class:`TimerError`。
    """
    raise NotImplementedError


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
    raise NotImplementedError
