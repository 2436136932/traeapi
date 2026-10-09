"""__main__.py traeapi 入口：加载配置 → 构建 pool → 起 HTTP 服务。

对应原版 cmd/server/main.go。

用法：

    python -m traeapi                     # 读 ./config.json（不存在则用默认值 + env）
    python -m traeapi -config my.json     # 指定配置文件

两个监听端口：
  - 主服务（默认 :7864）：OpenAI 兼容 API + 管理面板
  - 回调服务（默认 127.0.0.1:18080）：只处理 /authorize（TRAE 登录回调）
    端口被占用不致命，降级为「手动粘贴回调链接」模式。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

import uvicorn

from . import auth, config as config_mod, pidfile
from .pool import Pool
from .scheduler import Scheduler, SchedulerConfig
from .server.app import create_app
from .server.state import build_state
from .upstream import constants as C
from .upstream.client import Client

log = logging.getLogger("traeapi")


def _setup_logging() -> None:
    """日志走 stderr（对齐 Go log 包的行为，便于 start.ps1 收集 server.err.log）。"""
    _force_utf8_stdio()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y/%m/%d %H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # uvicorn 的 access log 噪音较大，降级到 warning
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _force_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8。

    Windows 控制台默认 GBK(cp936)，中文日志（账号昵称、上游中文错误提示）
    会直接抛 UnicodeEncodeError 或输出乱码。这里统一改写成 UTF-8 并不做替换丢弃。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数（兼容 Go flag 的 -config / --config 写法）。"""
    parser = argparse.ArgumentParser(prog="traeapi", add_help=True)
    parser.add_argument(
        "-config",
        "--config",
        dest="config",
        default="config.json",
        help="path to config json (default: config.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """程序入口。"""
    _setup_logging()
    args = parse_args(argv)

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        log.error("load config: %s", exc)
        return 1

    auths = auth.load_dir(cfg.auth_dir)
    log.info("loaded %d account(s) from %s", len(auths), cfg.auth_dir)

    pool = Pool(cfg.state_file)
    pool.sync_to_dir(auths)  # 对齐：剔除 state.json 中已删除 auth 文件的幽灵账号

    upstream = Client(timeout_seconds=cfg.upstream.timeout_seconds)

    # 应用 SOLO function 配置；未知取值保持默认并告警（避免上游 4001）。
    if cfg.solo_function and not C.set_function(cfg.solo_function):
        log.warning(
            "unknown solo_function %r, keep %s", cfg.solo_function, C.active_function()
        )
    log.info("solo function: %s", C.active_function())

    state = build_state(cfg, pool, upstream)
    app = create_app(state)

    scheduler = Scheduler(
        SchedulerConfig(
            pool=pool,
            upstream=upstream,
            checkin_hour=cfg.schedule.checkin_hour,
            refresh_hours=list(cfg.schedule.refresh_hours),
            refresh_skew=24 * 3600.0,
            checkin_retry=float(cfg.schedule.checkin_retry_minutes) * 60.0,
        )
    )
    scheduler.start()

    host, port = cfg.host_port()
    main_server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
            timeout_keep_alive=30,
        )
    )

    callback_server: uvicorn.Server | None = None
    callback_thread: threading.Thread | None = None
    cb = cfg.callback_host_port()
    if cb is not None:
        cb_host, cb_port = cb
        callback_server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=cb_host,
                port=cb_port,
                log_level="warning",
                access_log=False,
            )
        )

        def _run_callback() -> None:
            log.info(
                "traeapi callback server on %s:%s (TRAE login /authorize)", cb_host, cb_port
            )
            try:
                callback_server.run()
            except SystemExit:
                pass
            except OSError as exc:
                # 端口被占用（login.sh / 旧实例）不致命，降级为手动粘贴模式。
                log.warning(
                    "callback server (:%s) failed: %s — web 登录降级为手动粘贴回调链接",
                    cb_port,
                    exc,
                )

        callback_thread = threading.Thread(
            target=_run_callback, name="traeapi-callback", daemon=True
        )
        callback_thread.start()

    stopping = threading.Event()

    def _shutdown(signum, _frame) -> None:  # noqa: ANN001
        if stopping.is_set():
            return
        stopping.set()
        log.info("received signal %s, shutting down", signum)
        main_server.should_exit = True
        if callback_server is not None:
            callback_server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            # 非主线程 / 平台不支持时忽略
            pass

    log.info("traeapi listening on %s (api_key=%s)", cfg.listen, bool(cfg.api_key))
    # 自注册 PID 文件：无论由谁拉起（start.ps1 / start.sh / 手动 / WMI），
    # 状态都可被脚本追踪，避免「端口被占但脚本找不到进程」的死角。
    pid_path = pidfile.pid_file_for(cfg.state_file)
    pidfile.write_pid_file(pid_path)
    try:
        main_server.run()
    except OSError as exc:
        log.error("http: %s", exc)
        return 1
    finally:
        pidfile.remove_pid_file(pid_path)
        scheduler.stop()
        if callback_server is not None:
            callback_server.should_exit = True
        if callback_thread is not None:
            callback_thread.join(timeout=3.0)
        upstream.close()
        log.info("bye")
    return 0


if __name__ == "__main__":
    # 支持 `python -m traeapi` 与 `python traeapi/__main__.py` 两种方式
    raise SystemExit(main())
