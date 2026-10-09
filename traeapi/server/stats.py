"""stats.py 调用记录：内存环形缓冲，供面板「调用记录」页展示。

对应原版 internal/server/stats.go。

设计约束：
  - 只存内存（重启即清空），不落盘；只记录元信息，不含 prompt / 回复正文，
    避免用户对话内容留存在磁盘上。
  - 固定容量覆盖式写入，长时间运行内存占用恒定。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

# 调用记录保留条数上限（环形缓冲容量）。
USAGE_LOG_CAPACITY = 200


def usage_int(mapping: dict, key: str) -> int:
    """取 usage map 中的整数字段（JSON 解码后数值可能是 float）。"""
    value = mapping.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


@dataclass
class UsageRecord:
    """单次 /v1/chat/completions 调用的元信息。"""

    time: str = ""
    model: str = ""
    uid: str = ""
    nickname: str = ""
    stream: bool = False
    ok: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    err_code: str = ""
    err_msg: str = ""

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "time": self.time,
            "model": self.model,
            "stream": self.stream,
            "ok": self.ok,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "duration_ms": self.duration_ms,
        }
        if self.uid:
            out["uid"] = self.uid
        if self.nickname:
            out["nickname"] = self.nickname
        if self.err_code:
            out["err_code"] = self.err_code
        if self.err_msg:
            out["err_msg"] = self.err_msg
        return out


class UsageLog:
    """固定容量调用记录环形缓冲（并发安全）。"""

    def __init__(self, capacity: int = USAGE_LOG_CAPACITY) -> None:
        if capacity <= 0:
            capacity = USAGE_LOG_CAPACITY
        self._mu = threading.Lock()
        self.capacity = capacity
        self._buf: list[UsageRecord | None] = [None] * capacity
        self._next = 0  # 下一个写入槽位
        self._filled = 0  # 当前已写入条数（判满用）
        self._total = 0  # 累计调用数
        self._ok_count = 0
        self._created = time.time()

    def add(self, record: UsageRecord) -> None:
        """追加一条记录（超出容量时覆盖最旧的一条）。"""
        if not record.time:
            record.time = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._mu:
            self._buf[self._next] = record
            self._next = (self._next + 1) % self.capacity
            if self._filled < self.capacity:
                self._filled += 1
            self._total += 1
            if record.ok:
                self._ok_count += 1

    def recent(self, n: int = 0) -> list[UsageRecord]:
        """返回最近 n 条记录，最新在前；n <= 0 时返回全部保留记录。"""
        with self._mu:
            if n <= 0 or n > self._filled:
                n = self._filled
            out: list[UsageRecord] = []
            for i in range(n):
                # 从最新写入的位置往前取；加上 2*capacity 保证取模前恒为正
                idx = (self._next - 1 - i + self.capacity * 2) % self.capacity
                item = self._buf[idx]
                if item is not None:
                    out.append(item)
            return out

    def summary(self) -> dict:
        """汇总统计（对当前保留的记录求和，累计调用数单独给出）。"""
        with self._mu:
            prompt = completion = total = dur_sum = 0
            for i in range(self._filled):
                item = self._buf[i]
                if item is None:
                    continue
                prompt += item.prompt_tokens
                completion += item.completion_tokens
                total += item.total_tokens
                dur_sum += item.duration_ms
            avg = int(dur_sum / self._filled) if self._filled else 0
            return {
                "total_calls": self._total,
                "ok_calls": self._ok_count,
                "failed_calls": self._total - self._ok_count,
                "kept": self._filled,
                "capacity": self.capacity,
                "avg_duration_ms": avg,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "since": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._created)),
            }
