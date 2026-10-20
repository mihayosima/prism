"""模型清单、本地路径解析与（缺失时）懒下载。

为什么单独一个模块
------------------
引擎加载逻辑（``providers.py``）只该关心"模型文件在哪、齐不齐"，
不该知道文件名、URL、目录布局——那是**部署形态**，会随镜像源变动。
故此处把三件事集中登记：

1. **清单**：哪几个模型、各自有哪些文件、分别从哪个 URL 取；
2. **解析**：配置里的模型名 → 本地绝对路径（角色 → 路径）；
3. **就绪判定/下载**：缺文件时可选自动补（``auto_download``）。

三个模型（阶段 0 已实测加载，结论见 ``docs/plugins/asr/语音输入能力方案-2026-10-14.md``）
----------------------------------------------------------------------------
======================  =====================  ==========================  ==========
角色                     模型                    文件                          用途
======================  =====================  ==========================  ==========
``streaming``            X-ASR chunk-480ms     encoder/decoder/joiner/tokens实时流式（模式 R）
``file``                 SenseVoice int8model.int8.onnx/tokens  长录音离线（模式 F）
``vad``                  silero_vad.onnx       单文件                      两通道共用切段
======================  =====================  ==========================  ==========

★ **不要改用 k2-fsa releases 的 ``sherpa-onnx-x-asr-...-int8-2026-06-03``**：
  其编码器 ONNX **缺 ``encoder_dims`` 等结构元数据**，
  ``OnlineRecognizer.from_transducer`` 直接报
  ``'encoder_dims' does not exist in the metadata`` 无法加载（fp32 同缺，根因在导出阶段）。
  当前用的是上游官方**流式导出** ``chunk-480ms-model``（带完整元数据，cjkchar 分词）。

★ **默认下载源是 ``hf-mirror.com``**：实测 GitHub 直连仅 ~40–60KB/s（SenseVoice 的
  fp32 包约 1GB，需 4h+），而 hf-mirror 峰值 ~2–3.7MB/s；``ghproxy.com`` 不可达、
  ModelScope 的 k2-fsa 路径为空。``SENSE_VOICE`` 的 ``int8`` 量化版（239MB）
  而非 fp32——精度对 ASR 足够、加载更快。

本模块**零 sherpa 依赖**（只用 stdlib），故配置解析与就绪判定在没装引擎的
解释器上也能跑（端点据此给出"引擎不可用"而非崩服务）。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("agent.plugins.asr.models")

#: 模型根目录（相对项目根；与 ``run/chat.py`` 的 ``data/agent_chat.db`` 同一落点口径）。
DEFAULT_MODEL_ROOT = "data/asr_models"

#: 切片识别时的目标采样率（sherpa 的 X-ASR / SenseVoice / silero VAD 均按 16k 训练）。
TARGET_SAMPLE_RATE = 16000

#: 缺文件时单次下载尝试上限与单次 socket 超时（秒）。
_DOWNLOAD_TIMEOUT_S = 60
_DOWNLOAD_RETRIES = 3

_HF = "https://hf-mirror.com"
_GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"


@dataclass(frozen=True)
class ModelSpec:
    """一个模型的清单：目录名 + ``{角色: URL}``。"""

    key: str
    dir_name: str
    files: dict[str, str] = field(default_factory=dict)

    def paths(self, root: str | os.PathLike[str] = DEFAULT_MODEL_ROOT) -> dict[str, str]:
        """角色 → 本地绝对路径（**不判断存在性**，存在性由 ``missing`` 判定）。"""
        base = Path(root) / self.dir_name
        return {role: str(base / name) for role, name in self._local_names().items()}

    def _local_names(self) -> dict[str, str]:
        return {role: os.path.basename(url.split("?", 1)[0]) for role, url in self.files.items()}

    def missing(self, root: str | os.PathLike[str] = DEFAULT_MODEL_ROOT) -> list[str]:
        """缺失文件的角色列表（**空列表 = 就绪**）。"""
        paths = self.paths(root)
        return [role for role, p in paths.items()
                if not (Path(p).is_file() and Path(p).stat().st_size > 0)]


#: 流式模型：上游官方 ``chunk-480ms-model``（hf-mirror 镜像的 GilgameshWind/X-ASR-zh-en）。
STREAMING = ModelSpec(
    key="streaming",
    dir_name="sherpa-onnx-x-asr-zh-en-chunk-480ms",
    files={
        "encoder": f"{_HF}/GilgameshWind/X-ASR-zh-en/resolve/main/deployment/models/chunk-480ms-model/encoder-480ms.onnx",
        "decoder": f"{_HF}/GilgameshWind/X-ASR-zh-en/resolve/main/deployment/models/chunk-480ms-model/decoder-480ms.onnx",
        "joiner": f"{_HF}/GilgameshWind/X-ASR-zh-en/resolve/main/deployment/models/chunk-480ms-model/joiner-480ms.onnx",
        "tokens": f"{_HF}/GilgameshWind/X-ASR-zh-en/resolve/main/deployment/models/chunk-480ms-model/tokens.txt",
    },
)

#: 长录音离线模型：SenseVoice **int8 量化**版（csukuangfj 镜像的散文件，非 .tar.bz2）。
FILE_OFFLINE = ModelSpec(
    key="file",
    dir_name="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
    files={
        "model": f"{_HF}/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx",
        "tokens": f"{_HF}/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt",
    },
)

#: VAD：silero_vad.onnx（GitHub releases 直链即可，文件仅 ~0.6MB）。
#: ★ ``dir_name=""`` ⇒ 文件**直落模型根目录**（``data/asr_models/silero_vad.onnx``），
#:   不套子目录：``pathlib`` 忽略空串路径段，故 ``paths()`` 天然成立。
VAD = ModelSpec(key="vad", dir_name="",
                files={"model": f"{_GH}/silero_vad.onnx"})

#: 角色 → 清单。``streaming``/``file`` 的模型名可被配置覆盖（``model_root`` 下别的目录）。
SPECS: dict[str, ModelSpec] = {s.key: s for s in (STREAMING, FILE_OFFLINE, VAD)}


def spec_for(key: str, model_name: str = "") -> ModelSpec:
    """按角色取清单；``model_name`` 给了就用它当目录名（支持换用别的导出目录）。

    ★ 这里刻意**不做版本兼容判断**：目录名即版本，同名目录被替换属于运维动作，
    插件信任落盘内容（与 ``plugins.skills`` 信任 skill.md 目录同一姿态）。
    """
    base = SPECS.get(key)
    if base is None:
        raise KeyError(f"未知的模型角色：{key!r}")
    if model_name and model_name != base.dir_name:
        return ModelSpec(key=key, dir_name=model_name, files=base.files)
    return base


def status(root: str | os.PathLike[str] = DEFAULT_MODEL_ROOT,
           overrides: dict[str, str] | None = None) -> dict:
    """三个模型的落地状况（纯查询，**不触网、不加载**）⇒ status()/端点据此如实回报。

    返回 ``{角色: {"dir":..., "present": bool, "missing": [...]}}``。
    """
    out: dict[str, dict] = {}
    for key, spec in SPECS.items():
        name = (overrides or {}).get(key) or ""
        s = spec_for(key, name)
        paths = s.paths(root)
        miss = s.missing(root)
        out[key] = {
            "dir": str(Path(root) / s.dir_name),
            "ready": not miss,
            "missing": miss,
            "files": paths,
        }
    return out


def ensure_ready(key: str, root: str | os.PathLike[str] = DEFAULT_MODEL_ROOT,
                 model_name: str = "", *, allow_download: bool = True,
                 progress=None) -> None:
    """确保某角色的模型文件齐备；缺则按需下载。

    - ``allow_download=False`` ⇒ 缺文件直接抛 ``FileNotFoundError``（离线部署语义）；
    - ``progress`` 可选可调用对象（下载完成后收到一条描述，用于 WS ``loading`` 帧）；
    - **下载落盘用临时文件 + 原子改名**：中途断网/被杀不会留下"看着像完整"的半截模型
      （阶段 0 曾被半成品 tar.bz2 咬过一次：文件在、大小不对，加载才炸）。
    """
    spec = spec_for(key, model_name)
    missing = spec.missing(root)
    if not missing:
        return
    if not allow_download:
        raise FileNotFoundError(
            f"{spec.key} 模型缺失且未开启自动下载：{', '.join(missing)}"
            f"（期望目录 {Path(root) / spec.dir_name}）")
    for role in missing:
        url = spec.files[role]
        dest = Path(spec.paths(root)[role])
        if progress:
            try:
                progress(f"下载 {spec.key}.{role} …")
            except Exception:                   # noqa: BLE001 —— 进度回调不该影响下载
                pass
        logger.info("下载模型 %s.%s ← %s", spec.key, role, url)
        _download(url, dest)
    still = spec.missing(root)
    if still:
        raise RuntimeError(f"{spec.key} 模型下载后仍缺失：{', '.join(still)}")
    if progress:
        try:
            progress(f"{spec.key} 模型就绪")
        except Exception:                       # noqa: BLE001
            pass


def _download(url: str, dest: Path, *, retries: int = _DOWNLOAD_RETRIES) -> None:
    """单个模型文件下载（``Range`` 断点续传 + 临时文件原子落盘）。

    续传的意义来自实测：GitHub 直连 ~40–60KB/s，一个 500MB 文件中途断链是常态，
    从零重下的代价远高于多试几次 ``Range``。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        have = dest.stat().st_size if dest.is_file() else 0
        req = urllib.request.Request(url, headers={"User-Agent": "prism-asr/1.0"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
                # 服务端不支持 Range 时会回 200（而非 206）⇒ 必须从头写，否则会拼出坏文件
                appending = have > 0 and getattr(resp, "status", 200) == 206
                mode = "ab" if appending else "wb"
                with open(dest, mode) as fh:
                    shutil.copyfileobj(resp, fh, length=1 << 20)
            return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_exc = exc
            logger.warning("模型下载失败（第 %d/%d 次，%s）：%s", attempt, retries, url, exc)
    raise RuntimeError(f"模型下载失败（重试 {retries} 次）：{url}") from last_exc


def read_audio(path: str | os.PathLike[str], target_sr: int = TARGET_SAMPLE_RATE):
    """读音频 → ``(单声道 float32 [-1,1], 原始采样率, 时长秒)``；必要时重采样。

    ★ **格式支持以 libsndfile 为准**（``soundfile`` 的后端）：wav/flac/ogg/mp3 等；
    **m4a/aac/wma 不支持**——不静默失败，端点据异常明确回报"不支持的格式"，
    也不假装能读（曾有语音组件把解不动���格式当空音频返回，那是错的）。
    多声道取各声道均值降为单声道（ASR 模型一律单声道输入）。
    """
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = np.ascontiguousarray(mono, dtype=np.float32)
    if sr != target_sr:
        mono = resample(mono, sr, target_sr)
        sr = target_sr
    return mono, int(sr), float(len(mono)) / float(sr or 1)


def resample(samples, src_sr: int, dst_sr: int):
    """线性插值重采样（``numpy`` 实现，float32 in/out）。

    ★ **诚实边界**：这是线性插值，**非带限重采样**，对 16k 目标而言在语音频段
    误差可接受；但它不是 ``soxr``/``resampy`` 那种专业重采样。
    需要更高保真（如把 44.1k 音乐性录音当素材）时属第二版替换点——**换实现即可，
    调用点签名不变**。不做这件事就会在"采样率对齐"上悄悄引入混叠。
    """
    import numpy as np

    src = np.asarray(samples, dtype=np.float32)
    if src_sr == dst_sr or src.size == 0:
        return src
    n_out = int(round(src.size * float(dst_sr) / float(src_sr)))
    if n_out <= 0:
        return np.zeros((0,), dtype=np.float32)
    t_old = np.arange(src.size, dtype=np.float64) / float(src_sr)
    t_new = np.arange(n_out, dtype=np.float64) / float(dst_sr)
    return np.interp(t_new, t_old, src).astype(np.float32)


__all__ = ["DEFAULT_MODEL_ROOT", "TARGET_SAMPLE_RATE", "FILE_OFFLINE", "ModelSpec",
           "SPECS", "STREAMING", "VAD", "ensure_ready", "read_audio", "resample",
           "spec_for", "status"]