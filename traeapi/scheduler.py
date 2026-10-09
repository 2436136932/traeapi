"""scheduler.py 定时任务：每日签到 + token 预刷新。

对应原版 internal/scheduler/scheduler.go。
签到成功后重新查积分，积分 > 0 的冷却账号自动解冻。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .pool import Pool
from .upstream.client import Client, ErrKind, UpstreamError

log = logging.getLogger("traeapi.scheduler")

# 单个签到周期内最多自动重试次数，避免上游持续拒绝时无休止地刷请求。
MAX_CHECKIN_RETRIES = 12


@dataclass
class SchedulerConfig:
    """调度器依赖。"""

    pool: Pool
    upstream: Client
    checkin_hour: int = 9  # 每日签到小时，默认 9
    refresh_hours: list[int] = field(default_factory=lambda: [3])  # token 预刷新小时
    refresh_skew: float = 24 * 3600.0  # 预刷新窗口，默认 24h
    # 签到未能全部成功（如上游 9074「当前参与用户太多」）后的自动重试间隔；
    # 0 表示不重试。
    checkin_retry: float = 0.0


def next_fire(now: datetime, hours: list[int]) -> datetime:
    """返回 now 之后最近的一个整点触发时间；hours 为本地小时（0-23）。"""
    earliest: datetime | None = None
    for hour in hours:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate = candidate + timedelta(days=1)
        if earliest is None or candidate < earliest:
            earliest = candidate
    return earliest if earliest is not None else now


class Scheduler:
    """调度器。"""

    def __init__(self, cfg: SchedulerConfig) -> None:
        if cfg.checkin_hour < 0:
            cfg.checkin_hour = 9
        if not cfg.refresh_hours:
            cfg.refresh_hours = [3]
        if cfg.refresh_skew <= 0:
            cfg.refresh_skew = 24 * 3600.0
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """后台线程启动主循环。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name="traeapi-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """请求停止并等待线程退出。"""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> None:
        """主循环，阻塞直到 stop()。

        除整点触发外，若签到未能全部成功（如上游 9074 风控拒绝/高峰限流），
        会按 cfg.checkin_retry 的间隔自动重试，最多 MAX_CHECKIN_RETRIES 次。
        """
        hours = list(self.cfg.refresh_hours) + [self.cfg.checkin_hour]
        retry_at = 0.0
        retries = 0

        # 启动兜底：服务可能在当日签到时刻之后才启动（或刚重启），
        # 此时立即补签一次；未全部成功则直接进入重试流程。
        # 若当前尚未到签到时刻，则不做任何请求，等整点触发。
        if datetime.now().hour >= self.cfg.checkin_hour:
            if not self.run_checkin_now() and self.cfg.checkin_retry > 0:
                log.info(
                    "checkin: 启动检查发现未签到成功，%ss 后自动重试", self.cfg.checkin_retry
                )
                retry_at = time.time() + self.cfg.checkin_retry

        while not self._stop.is_set():
            now = datetime.now()
            target = next_fire(now, hours)
            wait = max(0.0, (target - now).total_seconds())

            # 等待到「整点触发」或「重试时刻」中较早的一个
            while wait > 0 and not self._stop.is_set():
                if retry_at and retry_at - time.time() < wait:
                    wait = max(0.0, retry_at - time.time())
                    break
                slice_ = min(wait, 1.0)
                if self._stop.wait(slice_):
                    return
                wait -= slice_
                now = datetime.now()
                wait = max(0.0, (target - now).total_seconds())

            if self._stop.is_set():
                return

            if retry_at and time.time() >= retry_at:
                retries += 1
                retry_at = 0.0
                if self.run_checkin_now():
                    log.info("checkin: 重试第 %d 次后全部成功", retries)
                    retries = 0
                elif retries >= MAX_CHECKIN_RETRIES:
                    log.info("checkin: 已重试 %d 次仍未全部成功，本次停止重试", retries)
                    retries = 0
                elif self.cfg.checkin_retry > 0:
                    retry_at = time.time() + self.cfg.checkin_retry
                continue

            hour = datetime.now().hour
            if hour in self.cfg.refresh_hours:
                self.run_refresh_now()
            if self.cfg.checkin_hour == hour:
                retry_at = 0.0
                retries = 0
                if not self.run_checkin_now() and self.cfg.checkin_retry > 0:
                    log.info(
                        "checkin: 有账号未签到成功，%ss 后自动重试", self.cfg.checkin_retry
                    )
                    retry_at = time.time() + self.cfg.checkin_retry

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------

    def run_checkin_now(self) -> bool:
        """立即对所有账号执行签到 + 积分刷新 + 解冻。

        冷却中的账号也参与（签到就是为了解冻它们）；禁用的跳过。
        返回是否全部账号都已签到成功 —— False 表示有账号失败（如上游 9074 风控拒绝、
        临时限流）或仍处于未签到状态，调用方可据此安排自动重试。
        """
        all_done = True
        for status in self.cfg.pool.list():
            if status.disabled:
                continue
            auth = self.cfg.pool.auth_by_uid(status.uid)
            if auth is None or not auth.refresh_token_value():
                continue

            # 签到（status → 未签到则 claim）
            try:
                checked_in, _, enable = self.cfg.upstream.checkin_status(auth)
            except Exception as exc:  # noqa: BLE001
                log.warning("checkin status %s: %s", status.uid, exc)
                all_done = False
            else:
                if checked_in:
                    log.info("checkin %s: already checked in", status.uid)
                elif not enable:
                    pass  # 该账号签到功能未开启，不视为失败
                else:
                    try:
                        self.cfg.upstream.checkin_claim(auth)
                        log.info("checkin %s: ok", status.uid)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("checkin claim %s: %s", status.uid, exc)
                        all_done = False

            # 查积分 + 解冻
            try:
                remain = self.cfg.upstream.user_ent_usage(auth)
            except Exception as exc:  # noqa: BLE001
                log.warning("ent-usage %s: %s", status.uid, exc)
                continue
            self.cfg.pool.reenable_if_credits(status.uid, remain)
        return all_done

    def run_refresh_now(self) -> None:
        """立即对所有账号刷新 token；session 失效的自动禁用。"""
        for status in self.cfg.pool.list():
            if status.disabled:
                continue
            auth = self.cfg.pool.auth_by_uid(status.uid)
            if auth is None or not auth.refresh_token_value():
                continue
            if not auth.needs_refresh(self.cfg.refresh_skew):
                continue
            try:
                self.cfg.upstream.refresh_token(auth)
            except Exception as exc:  # noqa: BLE001
                log.warning("refresh %s: %s", status.uid, exc)
                if isinstance(exc, UpstreamError) and exc.kind == ErrKind.SESSION_DEAD:
                    self.cfg.pool.disable(status.uid, "session dead")
                continue
            try:
                auth.save_atomic()
            except Exception as exc:  # noqa: BLE001 - 落盘失败不阻断调度（对齐 Go 的 `_ =`）
                log.warning("refresh %s save: %s", status.uid, exc)
