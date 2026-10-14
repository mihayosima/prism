"""Silero VAD 封装 —— 实时/离线两通道共用的**语音切段**机制。

为什么是共用件
--------------
"哪里是语音、哪里是静音"这个问题在两个通道里完全同构，差别只在喂入方式：

- **离线（长录音）**：整段读入 → 一次性切出 ``[(start, end), …]`` → 逐段送离线识别器；
- **实时（麦克风）**：音频增量到达 ⇒ 逐块 ``accept_waveform``，凑够一段就 ``pop`` 出来。

两件事若各写一份，切段参数（静音时长/最短语音时长）就会漂移——同一段录音
在两条通道上被切成不同边界，肉眼难辨的差异会在下游（章节总结、待办抽取）放大。
故**一个 ``VadModule`` 两种喂法**。

sherpa-onnx 1.13.8 的事实（★ 勿再假设 ``OfflineVad``）
---------------------------------------------------
本版本**没有** ``OfflineVad``，切段走 ``VoiceActivityDetector``：

    cfg = VadModelConfig(); cfg.silero_vad.model = <silero_vad.onnx>
    vad = VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
    vad.accept_waveform(整段 float32) ; vad.flush()
    while not vad.empty(): seg = vad.front(); …; vad.pop()

- ★ 构造参数名是 ``buffer_size_in_seconds``（**秒**），不是 ``buffer_size``（采样数）；
- ``SpeechSegment`` **只有** ``start``（秒）与 ``samples``，**没有** ``end``，
  故终点必须自己算：``start + len(samples) / sample_rate``；
- 采样率固定 16k（模型固有），非 16k 音频须先经 ``models.resample``。
"""

from __future__ import annotations

import logging
from typing import Iterator

from .models import TARGET_SAMPLE_RATE

logger = logging.getLogger("agent.plugins.asr.vad")


class VadModule:
    """``VoiceActivityDetector`` 的薄封装：构造参数集中、切段语义明确、可注入假实现。

    注入点（``sherpa`` 参数）存在的理由：单元测试不该真加载 VAD 模型——既慢又让
    契约依赖磁盘上的 onnx。传一个假模块即可穷举"切出零段/多段/静音全段"等分支。
    生产路径留空 → ``load()`` 时才 import ``sherpa_onnx``（**服务启动不因缺包而崩**）。
    """

    def __init__(self, model_path: str, *, sample_rate: int = TARGET_SAMPLE_RATE,
                 min_silence_duration: float = 0.25, min_speech_duration: float = 0.1,
                 buffer_size_in_seconds: float = 30.0, window_size: int = 512,
                 sherpa=None):
        self.model_path = str(model_path)
        self.sample_rate = int(sample_rate)
        self.min_silence_duration = float(min_silence_duration)
        self.min_speech_duration = float(min_speech_duration)
        self.buffer_size_in_seconds = float(buffer_size_in_seconds)
        self.window_size = int(window_size)
        self._sherpa = sherpa          # 注入的假模块（测试用）；None ⇒ 延迟 import
        self._vad = None

    # ------------------------------------------------------------------ 加载

    def load(self) -> None:
        """构造底层检测器（幂等）。

        异常原样抛出（``ImportError``/``RuntimeError``）——由上层 ``ASRService``
        转成"引擎不可用"的**可见**状态，不静默降级成"没有 VAD 也能跑"（不能：
        无 VAD 的实时通道会漏掉 endpoint，长录音会整段送识别导致精度崩）。
        """
        if self._vad is not None:
            return
        sherpa = self._sherpa
        if sherpa is None:
            import sherpa_onnx            # 延迟 import：缺包不该让服务起不来
            sherpa = sherpa_onnx
            self._sherpa = sherpa
        cfg = sherpa.VadModelConfig()
        cfg.silero_vad.model = self.model_path
        cfg.silero_vad.min_silence_duration = self.min_silence_duration
        cfg.silero_vad.min_speech_duration = self.min_speech_duration
        cfg.silero_vad.window_size = self.window_size
        self._vad = sherpa.VoiceActivityDetector(
            cfg, buffer_size_in_seconds=self.buffer_size_in_seconds)

    @property
    def loaded(self) -> bool:
        return self._vad is not None

    def reset(self) -> None:
        """清空内部缓冲（换会话/换音频源时必须调，否则上一段尾音会漏进下一段）。"""
        if self._vad is not None:
            self._vad.reset()

    # ------------------------------------------------------------- 离线切段

    def segment_file(self, samples, chunk_seconds: float = 1.0) -> list[tuple[float, float]]:
        """整段音频 → ``[(start_sec, end_sec), …]``（**时间戳升序**）。

        ★ **必须分块喂入**（这不是优化，是正确性要求）
        --------------------------------------------
        ``VoiceActivityDetector`` 内部是**固定容量环形缓冲**（实测 capacity=160000 采样
        ≈ 10 秒 @16k）。一次性 ``accept_waveform(整段长录音)`` 会**溢出**——
        C++ 侧打印 ``Overflow! ... Original data is copied. No data loss!``，
        而**已完成的语音段全部丢失**、只留下末尾残余段（实测：10.05s 音频整段灌入
        只切出末尾 0.38s；改 1s 分块则正确切出多段）。
        ⇒ 长录音通道（会议/访谈动辄几十分钟）若整段灌入，**转写结果会静默只剩一句**。

        ★ ``SpeechSegment.start`` 是**采样点索引**（int），**不是秒**
        --------------------------------------------
        实测值 ``158144`` 对应 10.05s 音频里的第 9.884s ⇒ 单位是采样点。
        把它当秒用会让所有切片落在音频之外 ⇒ 识别零段（曾真踩：长录音返回空结果）。
        终点仍要自己算：``start`` 无 ``end`` 字段。
        """
        self.load()
        self.reset()
        total = int(len(samples))
        step = max(1, int(float(chunk_seconds) * self.sample_rate))
        segs: list[tuple[float, float]] = []
        for offset in range(0, total, step):
            self._vad.accept_waveform(samples[offset:offset + step])
            # 每块喂完立刻取走：否则段留在缓冲里等到溢出
            while not self._vad.empty():
                seg = self._vad.front          # property（1.13.8）
                start = float(seg.start) / float(self.sample_rate)
                end = start + len(seg.samples) / float(self.sample_rate)
                segs.append((start, end))
                self._vad.pop()
        self._vad.flush()
        while not self._vad.empty():
            seg = self._vad.front              # property（1.13.8）
            start = float(seg.start) / float(self.sample_rate)
            end = start + len(seg.samples) / float(self.sample_rate)
            segs.append((start, end))
            self._vad.pop()
        # 环形缓冲可能吐出越界段（start 落在音频尾部之后）⇒ **丢弃**（不是夹紧：
        # 夹紧会造出 start=end 的零长段，识别器收到 0 长音频反而是错数据）。
        duration = total / float(self.sample_rate)
        kept = [(s, e) for s, e in segs if s < duration and e > s]
        kept.sort(key=lambda se: se[0])
        return kept

    def iter_segments(self, samples, min_len_sec: float = 0.0) -> Iterator[tuple[float, float]]:
        """``segment_file`` 的惰性版本，附带**最短段过滤**（默认不过滤）。

        过短的段（一声"嗯"、键盘声）识别出来几乎全是噪声文本，留在结果里
        只会污染下游摘要；``min_len_sec`` 让调用方按场景取舍。
        """
        for start, end in self.segment_file(samples):
            if (end - start) >= float(min_len_sec):
                yield start, end

    # ------------------------------------------------------------- 实时喂入

    def accept(self, samples) -> None:
        """增量喂一块音频（**不切段**，只累积）。"""
        self.load()
        self._vad.accept_waveform(samples)

    def flush(self) -> None:
        """喂完最后一块后调用，吐出尾部残留段。"""
        self.load()
        self._vad.flush()

    def pop_segment(self) -> tuple[float, float] | None:
        """取一段已完成的语音段；没有则 ``None``（实时通道的"断句"入口）。"""
        self.load()
        if self._vad.empty():
            return None
        seg = self._vad.front          # property（同上）
        self._vad.pop()
        start = float(seg.start)
        return start, start + len(seg.samples) / float(self.sample_rate)

    def is_speech_detected(self) -> bool:
        """当前是否检测到语音（未加载时按"无语音"处理，不抛）。"""
        if self._vad is None:
            return False
        try:
            return bool(self._vad.is_speech_detected())
        except Exception:                       # noqa: BLE001 —— 判据失败即视为无语音
            return False


__all__ = ["VadModule"]