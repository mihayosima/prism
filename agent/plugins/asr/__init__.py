"""插件包：asr —— 本地私有语音识别，纯离线。

定位
=====
给 Prism 装上"听"的能力：音频不出本机，
对外只暴露两个入口，不感知音频从哪来、文本给谁用。

两种模式
--------
- 模式 R：WS + int16 PCM 分片 → X-ASR chunk-480ms 流式出字。
- 模式 F：上传音频 → Silero VAD 切段 → SenseVoice int8 离线逐段
  识别 → ``Segment[]{index,start_sec,end_sec,text}``。

降级，仅模式 R
--------------
引擎不可用时由前端降级到浏览器
Web Speech API——后端不假装能用：``status()``/端点如实报 ``unavailable``+原因，
前端据此切换。
模式 F 无浏览器兜底，故只有明确报错。

与其它模块的关系
----------------
- 本插件只经 ``agent.api`` 取内核对象，不 import 其它插件、不碰内核私有模块；
- 它是自包含的独立插件：``data/agent_plugin_state.json`` 的 ``enabled`` 一删即
  卸载，其余功能行为零变化；``status()`` 不提供任何服务，
  端点经 ``ctx.root.get(Services.ASR)`` 软取——未装配备时端点 503 而非崩。

与"飞书/工作流"等下游编排完全无关：本插件只做"音频→文本"，
任何消费方都在插件边界之外，不在此登记任何下游契约。
"""

from __future__ import annotations

import logging

from agent.api import SESSION_CFG_VIEW, Services, current_session_key, inc_counter, plugin

from .providers import EngineUnavailable
from .service import (STATE_IDLE, STATE_LOADING, STATE_READY, STATE_UNAVAILABLE,
                      ASRService)

logger = logging.getLogger("agent.plugins.asr")

# : 插件默认配置。
DEFAULT_CONFIG: dict = {
    "provider": "sherpa",          # 首版唯一引擎；第二版在此登记 "sensevoice"/"hybrid"
    "sample_rate": 16000,          # X-ASR / SenseVoice / silero VAD 均按 16k 训练
    "num_threads": 2,              # onnxruntime 线程数
    "model_root": "data/asr_models",
    "auto_download": True,         # 缺模型时自动补
    "warmup_on_start": False,      # True 则启动后台预热，会读几百 MB，默认关
    "streaming": {
        "model": "sherpa-onnx-x-asr-zh-en-chunk-480ms",
        # 断句阈值：比官方 demo 调短，让实时出字更跟手
        "rule1_min_trailing_silence": 0.8,
        "rule2_min_trailing_silence": 1.2,
        "rule3_min_utterance_length": 20.0,
    },
    "file": {
        "model": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "min_segment_sec": 0.0,    # 过滤过短段的阈值
    },
    "vad": {
        "enabled": True,
        # 留空用清单默认落点；填入文件名会拼成 "<file>/<file>" 触发 FileExistsError
        "model": "",
        "min_silence_duration": 0.25,
        "min_speech_duration": 0.1,
        "buffer_size_in_seconds": 30.0,
    },
}


def _deep_merge(base: dict, overlay: dict | None) -> dict:
    """递归合并 dict 覆盖。"""
    out = dict(base)
    for k, v in (overlay or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _config(ctx) -> dict:
    """生效配置 = 插件默认 ← 容器级 ``agent.asr`` ← 会话级 ``plugins.asr``。

     ``SESSION_CFG_VIEW`` 是可调用对象 ``(section, sk) -> dict``，调用它即拿到
     已解包的插件节⇒ 不要再套
     一次 ``section_of``。与 ``plugins/sandbox`` 的 ``_config`` 同构。
     """
    merged = _deep_merge(DEFAULT_CONFIG, {})
    container = ((ctx.config or {}).get("agent") or {}).get("asr") or {}
    merged = _deep_merge(merged, container)
    sk = current_session_key(ctx)
    if not sk:
        return merged
    try:
        view = ctx.get(SESSION_CFG_VIEW)
        sess = view("asr", sk) if callable(view) else {}
    except Exception:                             # noqa: BLE001 —— 视图不可用即回落容器级
        return merged
    return _deep_merge(merged, sess)


@plugin
class ASR:
    """语音识别能力提供方——纯能力插件，不挂路由。

     端点在 ``run/chat.py`` 注册并经 ``ctx.root.get(Services.ASR)``
     软取本插件提供的门面；插件未启用 ⇒ 取不到 ⇒ 端点 503 明示。
     """

    name = "plugins.asr"
    version = "0.1.0"
    layer = "business"
    provides = [Services.ASR]
    # 工具暴露：注册 ``asr.transcribe_file`` 需要内核任务注册表，
    # 故声明注入 TASKS。
    inject = [Services.TASKS]
    tables = ()                      # 无持久表：识别结果经端点返回，不落库

    async def apply(self, ctx) -> None:
        self._ctx = ctx
        self._service = ASRService(_config(ctx))

        # provide 的是实例，
        # 不是 sandbox 那种"接缝工厂函数"——sandbox 提供的是 ``(session_key) -> dict``
        # 的惰性工厂，而本插件的重资源懒加载已经由 ``ASRService.ensure_ready``
        # 内部承担，端点要的就是可直接调用的门面。给函数会让
        # ``ctx.get(Services.ASR).status()`` 直接 AttributeError。
        ctx.provide(Services.ASR, self._service)

        # 暴露为 agent 工具。
        # - owner=self.name ⇒ 工具域 ``plugins.asr``，可在 ``tool_domains`` 白名单裁剪；
        # - risk="read"：仅读取文件、不改任何状态；
        # - handler 走 ``transcribe_path``，引擎未就绪时
        # 由 ``ASRService.ensure_ready`` 抛 ``EngineUnavailable``，不静默、不谎报。
        async def _transcribe_file(path: str, min_segment_sec: float = 0.0) -> dict:
            result = await self._service.transcribe_path(
                path, {"min_segment_sec": float(min_segment_sec or 0.0)})
            return result.to_dict()

        ctx.tasks.register(
            ctx, "asr.transcribe_file",
            schema={
                "path": {"type": "str", "required": True,
                         "description": "音频文件绝对路径或相对项目根的路径"},
                "min_segment_sec": {"type": "float", "required": False, "default": 0.0,
                                   "description": "过滤短于该秒数的语音段（默认 0 不过滤）"},
            },
            handler=_transcribe_file,
            owner=self.name,
            description=(
                "转写本地音频文件为带时间戳的文本段列表。"
                "支持 wav/flac/ogg/mp3 等（以 libsndfile 为准）；m4a/aac/wma 不支持会报错。"
                "返回 {duration_sec, sample_rate, mode, segments:[{index,start_sec,end_sec,text}]}。"
                "仅读取文件、不修改任何状态。"
            ),
            risk="read",
        )

        def _dispose() -> None:
            self._service.dispose()

        ctx.effect(_dispose)

        if bool(_config(ctx).get("warmup_on_start")):
            import asyncio
            asyncio.create_task(self._service.warmup())

    def service(self, ctx) -> ASRService:
        """当前服务实例。"""
        return self._service

    async def status(self, ctx) -> dict:
        """运行态摘要⇒ 插件页/前端可见。

         如实报告引擎可用性与缺口：``state=unavailable`` 时附 ``reason``，
         前端据此决定是否降级到 Web Speech。不谎报 "ready"。
         """
        info = self._service.status()
        state = info.get("state")
        if state == STATE_UNAVAILABLE:
            inc_counter("agent_asr_unavailable_total")
        return {
            "state": state,
            "reason": info.get("reason", ""),
            "streaming": bool((info.get("provider") or {}).get("streaming")),
            "file": bool((info.get("provider") or {}).get("file")),
            "provider": (info.get("provider") or {}).get("name", self.name),
            "models": info.get("models", {}),
            # 明确的能力矩阵：前端据此决定显示"实时语音输入"入口
            "capabilities": {
                "streaming": state == STATE_READY,
                "file": state == STATE_READY,
                "web_speech_fallback": True,   # 模式 R 的降级由前端承担
            },
            "config": _config(ctx),
        }


__all__ = ["ASR", "ASRService", "DEFAULT_CONFIG", "EngineUnavailable",
           "STATE_IDLE", "STATE_LOADING", "STATE_READY", "STATE_UNAVAILABLE"]
