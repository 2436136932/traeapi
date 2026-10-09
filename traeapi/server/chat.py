"""chat.py POST /v1/chat/completions：挑号 + 轮换 + 流式/非流式转换。

对应原版 internal/server/handler.go 的 chatCompletions / handleStreamError。

线程模型：路由函数是 async（需要 await 读 body），所有阻塞的上游 I/O 通过
`run_in_threadpool` 丢到线程池执行，等价于 Go 里「每个请求一个 goroutine」的语义，
不会阻塞事件循环。
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Iterator

from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .. import jsonutil
from ..pool import CoolKind
from ..upstream.client import (
    ErrKind,
    SOLOStreamError,
    UpstreamError,
    aggregate,
    classify,
    iter_openai_sse,
)
from .state import MAX_BODY_BYTES, ServerState
from .stats import UsageRecord, usage_int

log = logging.getLogger("traeapi.chat")

_STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def write_openai_error(status: int, code: str, msg: str) -> JSONResponse:
    """OpenAI 风格错误信封。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": msg, "type": "api_error", "code": code}},
    )


def set_model_in_body(body: bytes, config_name: str) -> bytes:
    """将 body 中 model 字段替换为 config_name，并返回改写后的 body。"""
    try:
        obj = jsonutil.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(obj, dict):
        return body
    obj["model"] = config_name
    try:
        return jsonutil.dumps(obj).encode("utf-8")
    except (TypeError, ValueError):
        return body


def handle_stream_error(state: ServerState, uid: str, err: SOLOStreamError) -> None:
    """流式响应中的上游业务错误 → pool 冷却状态机。

    1005 plan 权益不足 → 长冷却；其余（5xx/参数错误等）→ 累计错误冷却。
    """
    if err.kind() == ErrKind.PLAN_LIMIT.value:
        state.pool.cooldown(uid, CoolKind.PLAN, state.plan_cooldown, "plan 权益不足")
    else:
        state.pool.note_error(uid, state.err_threshold, state.err_cooldown)


class _Recorder:
    """调用记录：非流式在请求结束时落库，流式在流真正结束后落库。"""

    def __init__(self, state: ServerState, model: str, stream: bool) -> None:
        self.state = state
        self.record = UsageRecord(model=model, stream=stream)
        self.started = time.time()
        self.deferred = False
        self.done = False

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        self.record.duration_ms = int((time.time() - self.started) * 1000)
        if not self.record.ok and not self.record.err_code:
            self.record.err_code = "error"
        self.state.stats.add(self.record)


async def chat_completions(request: Any, state: ServerState) -> Response:
    """处理一次对话请求。"""
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        return write_openai_error(413, "request_too_large", "request body exceeds 8MB limit")

    peek: dict[str, Any] = {}
    if raw_body:
        try:
            parsed = jsonutil.loads(raw_body)
            if isinstance(parsed, dict):
                peek = parsed
        except (ValueError, TypeError):
            peek = {}

    is_stream = bool(peek.get("stream"))
    model_raw = peek.get("model")
    model_raw = model_raw if isinstance(model_raw, str) else ""

    recorder = _Recorder(state, model_raw, is_stream)
    try:
        result = await run_in_threadpool(_chat_sync, state, raw_body, model_raw, is_stream, recorder)
    finally:
        if not recorder.deferred:
            recorder.finish()

    if isinstance(result, StreamingResponse):
        return result
    return result


def _chat_sync(
    state: ServerState,
    raw_body: bytes,
    model_raw: str,
    is_stream: bool,
    recorder: _Recorder,
) -> Response:
    """阻塞路径：模型映射 + 挑号轮换 + 上游转发（在线程池里执行）。"""
    rec = recorder.record

    try:
        config_name = state.catalog.map_model(model_raw, state.pick_account)
    except ValueError as exc:
        rec.err_code, rec.err_msg = "invalid_model", str(exc)
        return write_openai_error(400, "invalid_request", str(exc))

    body = set_model_in_body(raw_body, config_name)

    tried: set[str] = set()
    last_err: Exception | None = None

    for _ in range(state.max_rotate):
        acct = state.pool.pick_excluding(tried)
        if acct is None:
            break
        tried.add(acct.uid)

        # token 临近过期 → 先 refresh（持锁重查，避免并发重复轮换；失败冷却换号）
        try:
            refreshed = state.upstream.refresh_token_if_needed(acct, state.refresh_skew)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if isinstance(exc, UpstreamError) and exc.kind == ErrKind.SESSION_DEAD:
                state.pool.disable(acct.uid, "refresh session dead")
            else:
                state.pool.cooldown(
                    acct.uid, CoolKind.ERR, state.err_cooldown, f"refresh: {exc}"
                )
            continue
        if refreshed:
            try:
                acct.save_atomic()
            except Exception as exc:  # noqa: BLE001 - 落盘失败不阻断请求（对齐 Go 的 `_ =`）
                log.warning("save refreshed token for %s failed: %s", acct.uid, exc)

        resp, status, resp_body, transport_err = state.upstream.chat_stream(acct, body)
        if transport_err is not None:
            last_err = transport_err
            state.pool.note_error(acct.uid, state.err_threshold, state.err_cooldown)
            continue

        if status >= 400:
            text = resp_body.decode("utf-8", errors="replace")
            kind = classify(status, text)
            last_err = UpstreamError(kind, status, text)
            if kind == ErrKind.PLAN_LIMIT:
                state.pool.cooldown(
                    acct.uid, CoolKind.PLAN, state.plan_cooldown, "plan 权益不足"
                )
            elif kind == ErrKind.SOFT_RATE:
                state.pool.cooldown(
                    acct.uid, CoolKind.SOFT, state.soft_cooldown, "429 rate limit"
                )
            elif kind == ErrKind.SESSION_DEAD:
                state.pool.disable(acct.uid, "session dead")
            elif kind == ErrKind.NOT_FOUND:
                # 404 短冷却不累计 errCount（防雪崩）
                state.pool.cooldown(
                    acct.uid, CoolKind.SOFT, state.soft_cooldown, "upstream 404"
                )
            else:
                state.pool.note_error(acct.uid, state.err_threshold, state.err_cooldown)
            continue

        rec.uid, rec.nickname = acct.uid, acct.nickname

        if is_stream:
            recorder.deferred = True
            return _build_stream_response(state, acct, resp, recorder)

        # 非流式：聚合上游 SSE
        try:
            result = aggregate(state.upstream.iter_stream_bytes(resp))
        except SOLOStreamError as exc:
            last_err = exc
            if exc.kind() == ErrKind.PLAN_LIMIT.value:
                state.pool.cooldown(
                    acct.uid, CoolKind.PLAN, state.plan_cooldown, "plan 权益不足"
                )
            else:
                state.pool.note_error(acct.uid, state.err_threshold, state.err_cooldown)
            continue
        except Exception as exc:  # noqa: BLE001
            rec.err_code, rec.err_msg = "upstream_parse", str(exc)
            return write_openai_error(502, "upstream_parse", str(exc))
        finally:
            resp.close()

        state.pool.note_success(acct.uid)
        rec.ok = True
        usage = result.get("usage")
        if isinstance(usage, dict):
            rec.prompt_tokens = usage_int(usage, "prompt_tokens")
            rec.completion_tokens = usage_int(usage, "completion_tokens")
            rec.total_tokens = usage_int(usage, "total_tokens")
        return JSONResponse(status_code=200, content=jsonutil.normalize_numbers(result))

    msg = "all accounts unavailable (cooling/disabled)"
    if last_err is not None:
        msg += f": {last_err}"
    rec.err_code, rec.err_msg = "no_healthy_account", msg
    return write_openai_error(503, "no_healthy_account", msg)


def _build_stream_response(
    state: ServerState, acct, resp, recorder: _Recorder
) -> StreamingResponse:
    """构造流式响应：SSE 透传转换，每个 chunk 立即 flush。"""
    rec = recorder.record

    def on_error(err: SOLOStreamError) -> None:
        rec.ok = False
        rec.err_code = f"solo_{err.code}"
        rec.err_msg = err.msg
        handle_stream_error(state, acct.uid, err)

    def on_usage(usage: dict) -> None:
        rec.prompt_tokens = usage_int(usage, "prompt_tokens")
        rec.completion_tokens = usage_int(usage, "completion_tokens")
        rec.total_tokens = usage_int(usage, "total_tokens")

    state.pool.note_success(acct.uid)
    rec.ok = True

    source: Iterator[str] = iter_openai_sse(
        state.upstream.iter_stream_bytes(resp), on_error, on_usage
    )
    sentinel = object()

    def next_chunk() -> Any:
        """在线程池里拉下一个 chunk（阻塞的上游读取不占用事件循环）。"""
        try:
            return next(source)
        except StopIteration:
            return sentinel

    async def generate() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await run_in_threadpool(next_chunk)
                if chunk is sentinel:
                    break
                yield chunk.encode("utf-8")
        finally:
            try:
                resp.close()
            finally:
                recorder.finish()

    return StreamingResponse(
        generate(),
        status_code=200,
        media_type="text/event-stream",
        headers=_STREAM_HEADERS,
    )
