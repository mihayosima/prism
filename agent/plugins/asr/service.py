"""``ASRService`` —— 插件的**单一门面**：懒加载引擎、串行化重资源、暴露双通道入口。

为什么需要这一层（provider 之上的三件事）
----------------------------------------
1. **懒加载**（`ensure_ready`）：识别器加载要读 565MB encoder，**绝不能**在
   插件 ``apply``（服务启动/装配期）里做——否则 ASR 插件会把整个服务的启动
   拖慢几秒到十几秒，且模型缺失会**阻断装配**。故这里做成"首次真实调用时才加载"，
   并用状态机把"未加载/加载中/就绪/不可用"如实暴露给端点与 status。
2. **串行化重资源**：``OfflineRecognizer.decode`` 不是线程安全的；WS 实时流与
   文件长录音**共用**同一批识别器 ⇒ 文件转写必须拿锁串行，避免并发 decode 崩引擎。
3. **线程池隔离**：onnxruntime 的 ``decode`` 是 **CPU 密集同步调用**，放事件循环里
   会**阻塞整个服务**（其它 WS/HTTP 全卡）。故所有识别动作都 ``run_in_executor`` 丢线程池。

★ 状态机（对齐 sandbox 的 fail-closed 诚实性）：``unavailable`` 时端点返回明确错误，
绝不让前端等一个"永远不来"的 loading。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import models as _models
from .providers import (ASRProvider, EngineUnavailable, FileTranscribeResult,
                        SherpaOnnxProvider, StreamSession)

logger = logging.getLogger("agent.plugins.asr.service")

#: 引擎状态（``status()`` 的 ``state`` 字段取值）。
STATE_IDLE = "idle"            # 未加载（引擎在等第一次真实调用）
STATE_LOADING = "loading"      # 正在加载
STATE_READY = "ready"          # 就绪
STATE_UNAVAILABLE = "unavailable"  # 不可用（缺包/缺模型），附 reason


class ASRService:
    """语音识别能力门面。``ASR`` 插件把本类实例注册为 ``Services.ASR``。"""

    def __init__(self, config: dict, *, provider: ASRProvider | None = None,
                 provider_factory=None, executor: ThreadPoolExecutor | None = None):
        self.config = dict(config or {})
        self._factory = provider_factory or SherpaOnnxProvider
        self._provider: ASRProvider | None = provider
        self._state = STATE_IDLE
        self._reason = ""
        self._loading_lock = asyncio.Lock()   # 防止并发首次调用重复加载两个大模型
        self._infer_lock = threading.Lock()   # 串行化 decode（跨线程；见模块注释）
        # 单线程 executor：识别任务本身已内部并行（num_threads），再叠并发只会抢 CPU
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="asr")

    # ------------------------------------------------------------- 状态自述

    @property
    def provider(self) -> ASRProvider | None:
        return self._provider

    @property
    def state(self) -> str:
        return self._state

    def status(self) -> dict:
        """运行态摘要（插件 ``status()``/设置页消费）——**如实**反映引擎可用性。"""
        info = self._provider.describe() if self._provider is not None else {
            "name": getattr(self._factory, "name", "sherpa"),
            "streaming": True, "file": True, "ready": False,
            "reason": "引擎尚未加载（首次调用时懒加载）",
        }
        return {
            "state": self._state,
            "reason": self._reason or info.get("reason", ""),
            "provider": info,
            "models": _models.status(
                str(self.config.get("model_root") or _models.DEFAULT_MODEL_ROOT),
                overrides={
                    "streaming": str((self.config.get("streaming") or {}).get("model") or ""),
                    "file": str((self.config.get("file") or {}).get("model") or ""),
                    # ★ vad 也要纳入：漏了它会导致 status 报"模型就绪"而实际加载失败
                    #   （两者用不同的 spec 解析路径，历史上正是这样错位的）。
                    "vad": str((self.config.get("vad") or {}).get("model") or ""),
                }),
        }

    # ------------------------------------------------------------- 懒加载

    async def ensure_ready(self) -> ASRProvider:
        """确保引擎就绪并返回 provider；不可用抛 ``EngineUnavailable``（**不静默**）。"""
        if self._state == STATE_READY and self._provider is not None:
            return self._provider
        async with self._loading_lock:
            # 双检：等锁期间可能已被别的协程加载好
            if self._state == STATE_READY and self._provider is not None:
                return self._provider
            provider = self._provider
            if provider is None:
                provider = self._factory(self.config)
            self._state = STATE_LOADING
            self._reason = ""
            loop = asyncio.get_running_loop()
            try:
                # load() 可能读几百 MB + 首次解码预热 ⇒ 丢线程池，别阻塞事件循环
                await loop.run_in_executor(self._executor, provider.load)
            except EngineUnavailable as exc:
                self._state = STATE_UNAVAILABLE
                self._reason = str(exc)
                self._provider = provider      # 保留（describe 仍能报出缺口）
                raise
            except Exception as exc:          # noqa: BLE001 —— 任何异常都转成不可用
                self._state = STATE_UNAVAILABLE
                self._reason = f"引擎加载失败：{exc}"
                self._provider = provider
                logger.exception("ASR 引擎加载失败")
                raise EngineUnavailable(self._reason) from exc
            self._provider = provider
            self._state = STATE_READY
            return provider

    # ------------------------------------------------------------- 模式 F

    async def transcribe_file(self, samples, sample_rate: int,
                              opts: dict | None = None) -> FileTranscribeResult:
        """长录音转写（模式 F）。CPU 密集 ⇒ 线程池 + 串行锁。"""
        provider = await self.ensure_ready()
        if not provider.file_supported:
            raise EngineUnavailable("当前 provider 不支持长录音转写")
        loop = asyncio.get_running_loop()

        def _run() -> FileTranscribeResult:
            with self._infer_lock:            # decode 非线程安全 ⇒ 串行
                return provider.transcribe_file(samples, sample_rate, opts)

        return await loop.run_in_executor(self._executor, _run)

    async def transcribe_path(self, path: str, opts: dict | None = None) -> FileTranscribeResult:
        """从文件路径转写：读音频（soundfile）+ 重采样到 16k，再走 :meth:`transcribe_file`。"""
        samples, sr, _duration = _models.read_audio(
            path, _models.TARGET_SAMPLE_RATE)
        return await self.transcribe_file(samples, sr, opts)

    # ------------------------------------------------------------- 模式 R

    def open_stream(self) -> StreamSession:
        """新建实时会话（模式 R）。

        ★ 同步方法：``create_stream`` 本身很轻（建一个空 stream），**真正的重活
        （模型加载）已在 :meth:`ensure_ready` 里完成**。WS 端点在 accept 前先
        ``await ensure_ready()``，故此处无 IO。
        """
        if self._provider is None or self._state != STATE_READY:
            raise EngineUnavailable(self._reason or "流式引擎未就绪")
        session = self._provider.create_stream_session()
        if session is None:
            raise EngineUnavailable("当前 provider 不支持流式识别")
        return session

    async def feed_stream(self, session: StreamSession, pcm_int16: bytes,
                          sample_rate: int = _models.TARGET_SAMPLE_RATE) -> list[dict]:
        """把一块 int16 PCM 喂进实时会话，返回本次帧（线程池执行）。"""
        loop = asyncio.get_running_loop()

        def _run() -> list[dict]:
            with self._infer_lock:
                return session.push(pcm_int16, sample_rate)

        return await loop.run_in_executor(self._executor, _run)

    async def finish_stream(self, session: StreamSession) -> list[dict]:
        loop = asyncio.get_running_loop()

        def _run() -> list[dict]:
            with self._infer_lock:
                return session.finish()

        return await loop.run_in_executor(self._executor, _run)

    async def warmup(self) -> bool:
        """预热（可选，进插件后台任务）：能加载就加载，失败仅记录——**不阻断启动**。"""
        try:
            await self.ensure_ready()
            return True
        except EngineUnavailable as exc:
            logger.info("ASR 预热跳过：%s", exc)
            return False

    def dispose(self) -> None:
        """插件卸载时收尾：关线程池（**幂等**，重复装配/热重载安全）。"""
        try:
            self._executor.shutdown(wait=False)
        except Exception:                       # noqa: BLE001 —— 关池失败不该阻断卸载
            pass


__all__ = ["ASRService", "STATE_IDLE", "STATE_LOADING", "STATE_READY",
           "STATE_UNAVAILABLE"]