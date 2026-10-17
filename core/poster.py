"""奖品配图（海报）的取用、压缩与落盘。

管理员给一场抽奖配图有两种输入方式：

* **图片附件**：回复一张图再发命令，或把图片段和命令写在同一条消息里；
* **图片链接**：在命令后面直接写 ``https://…``。

两条路最终都收敛到同一份实现：先拿到原始字节，压缩后落到
``data/plugin_data/<插件名>/covers/<群号>-<期号>.<后缀>``，数据库里只存**相对
路径**——这样以后搬走数据目录，老场次的配图依然跟着走。

发送时不需要关心路径能不能被协议端读到：aiocqhttp 适配器会把 ``Image`` 组件
统一转成 ``base64://`` 再交给协议端（见 AstrBot
``aiocqhttp_message_event.py`` 的 ``_from_segment_to_dict``），所以 NapCat 和
AstrBot 不共享文件系统也没关系。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:  # Pillow 用于把手机截图压到适合发群聊的体积
    from PIL import Image as PILImage
    from PIL import ImageOps as PILImageOps
except Exception:  # pragma: no cover - 取决于运行环境
    PILImage = None
    PILImageOps = None

# 配图目录名（相对插件数据目录）
COVERS_DIRNAME = "covers"

# 原始字节上限：超过这个体积直接拒绝，避免把内存和带宽吃满
MAX_SOURCE_BYTES = 12 * 1024 * 1024

# 压缩后仍超过这个体积就认为不适合发群聊（base64 还要再涨 1/3）
MAX_STORED_BYTES = 2 * 1024 * 1024

# 压缩目标：最长边 1280、JPEG 质量 85 对群聊足够清楚
MAX_EDGE = 1280
JPEG_QUALITY = 85

# 压缩后仍然超标时的二次压缩参数
FALLBACK_EDGE = 960
FALLBACK_QUALITY = 75

_RESAMPLE = getattr(PILImage, "Resampling", PILImage).LANCZOS if PILImage else None

# 常见图片格式的文件头
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)


def sniff_ext(data: bytes) -> str | None:
    """按文件头判断图片格式，认不出来返回 ``None``。

    不信任扩展名和 Content-Type——只有魔术字节能说明真实格式。
    """
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def parse_url(text: str) -> str | None:
    """从命令参数里认出图片链接。"""
    value = (text or "").strip()
    lowered = value.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return value
    return None


def _has_alpha(img: Any) -> bool:
    if img.mode in ("RGBA", "LA"):
        return True
    if img.mode == "P":
        return "transparency" in img.info
    return False


def _frame_count(data: bytes) -> int:
    """图片帧数；打不开就返回 0（交给后续编码报错）。"""
    if PILImage is None:
        return 0
    try:
        with PILImage.open(io.BytesIO(data)) as img:
            return int(getattr(img, "n_frames", 1) or 1)
    except Exception:
        return 0


def _encode(data: bytes, edge: int, quality: int) -> tuple[bytes, str]:
    """按给定边长与质量重新编码，返回 ``(字节, 后缀)``。

    保留透明通道（存 PNG），否则统一转 JPEG。
    """
    assert PILImage is not None
    with PILImage.open(io.BytesIO(data)) as img:
        prepared = PILImageOps.exif_transpose(img) or img
        prepared.load()
        prepared.thumbnail((edge, edge), _RESAMPLE)
        buffer = io.BytesIO()
        if _has_alpha(prepared):
            prepared.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue(), "png"
        if prepared.mode != "RGB":
            prepared = prepared.convert("RGB")
        prepared.save(
            buffer,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
        )
        return buffer.getvalue(), "jpg"


def compress_image(data: bytes) -> tuple[bytes, str]:
    """把图片压到适合发群聊的体积，返回 ``(字节, 后缀)``。

    Args:
        data: 原始图片字节。

    Returns:
        压缩后的字节与对应的文件后缀。

    Raises:
        ValueError: 图片无法识别或已损坏。
    """
    sniffed = sniff_ext(data)
    if PILImage is None or PILImageOps is None:
        # 没装 Pillow 也要能用：只做格式校验，体积交给调用方把关
        if sniffed is None:
            raise ValueError("无法识别的图片格式（支持 png / jpg / gif / webp / bmp）")
        logger.warning("[群抽奖] 未安装 Pillow，跳过配图压缩")
        return data, sniffed

    if _frame_count(data) > 1:
        # 动图重编码会掉帧，原样保留
        return data, sniffed or "gif"

    try:
        packed, ext = _encode(data, MAX_EDGE, JPEG_QUALITY)
    except Exception as exc:
        raise ValueError(f"图片无法识别或已损坏：{exc}") from exc

    if len(packed) > MAX_STORED_BYTES:
        try:
            packed, ext = _encode(data, FALLBACK_EDGE, FALLBACK_QUALITY)
        except Exception as exc:  # pragma: no cover - 首次编码已成功过
            raise ValueError(f"图片压缩失败：{exc}") from exc
    return packed, ext


def covers_dir(data_dir: str | Path) -> Path:
    """配图目录。"""
    return Path(data_dir) / COVERS_DIRNAME


def rel_path(group_id: str, seq: int, ext: str) -> str:
    """数据库里保存的相对路径。"""
    return f"{COVERS_DIRNAME}/{group_id}-{int(seq)}.{ext}"


def _file_stem(group_id: str, seq: int) -> str:
    return f"{group_id}-{int(seq)}"


def resolve_path(data_dir: str | Path, rel: str) -> Path | None:
    """把相对路径还原成真实路径；越界或文件不存在返回 ``None``。"""
    name = (rel or "").strip()
    if not name:
        return None
    if "/" in name or "\\" in name or ".." in name:
        # 只接受纯文件名，杜绝路径穿越
        name = Path(name).name
    target = covers_dir(data_dir) / name
    return target if target.is_file() else None


def save_poster(
    data_dir: str | Path,
    group_id: str,
    seq: int,
    data: bytes,
) -> str:
    """压缩并保存配图，返回数据库要存的相对路径。

    Args:
        data_dir: 插件数据目录。
        group_id: 群号（用于命名）。
        seq: 群内期号（用于命名）。
        data: 原始图片字节。

    Returns:
        相对路径，例如 ``covers/123456-3.jpg``。

    Raises:
        ValueError: 图片为空、无法识别、或压缩后依然过大。
    """
    if not data:
        raise ValueError("没有读到图片内容（图片是空的，或者下载失败）")
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(
            f"图片太大了（{len(data) / 1024 / 1024:.1f}MB），"
            f"请先压到 {MAX_SOURCE_BYTES // 1024 // 1024}MB 以内再发",
        )

    packed, ext = compress_image(data)
    if len(packed) > MAX_STORED_BYTES:
        raise ValueError(
            f"图片压缩后仍有 {len(packed) / 1024 / 1024:.1f}MB，太大无法发送，"
            "请换一张更小的图",
        )

    directory = covers_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    delete_poster(data_dir, group_id, seq)  # 换图时清掉旧后缀的残留
    (directory / f"{_file_stem(group_id, seq)}.{ext}").write_bytes(packed)
    return rel_path(group_id, seq, ext)


def delete_poster(data_dir: str | Path, group_id: str, seq: int) -> int:
    """删掉一场抽奖的配图（含可能存在的其它后缀），返回删除的文件数。"""
    directory = covers_dir(data_dir)
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.glob(f"{_file_stem(group_id, seq)}.*"):
        try:
            path.unlink()
            removed += 1
        except OSError as exc:  # pragma: no cover - 权限等异常不该影响抽奖
            logger.warning(f"[群抽奖] 配图删除失败（{path.name}）：{exc}")
    return removed


def sweep(data_dir: str | Path, keep_stems: set[str]) -> int:
    """删掉不再被任何场次引用的配图，返回删除数量。

    历史裁剪、手动清空、发送中途崩溃都会留下孤儿文件，发布时顺手扫一遍即可。

    Args:
        data_dir: 插件数据目录。
        keep_stems: 仍然有效的文件名（不含后缀）集合，形如 ``{"123456-3"}``。
    """
    directory = covers_dir(data_dir)
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.iterdir():
        if not path.is_file() or path.stem in keep_stems:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:  # pragma: no cover - 权限等异常不该影响抽奖
            logger.warning(f"[群抽奖] 配图清理失败（{path.name}）：{exc}")
    return removed


def _is_image(component: Any) -> bool:
    """判断消息组件是不是图片（兼容 Enum 与纯字符串两种 type）。"""
    value = getattr(component, "type", None)
    if value is None:
        return False
    text = getattr(value, "value", value)
    return str(text).lower() == "image"


def _first_image(components: Any) -> Any | None:
    for component in components or []:
        if _is_image(component):
            return component
    return None


def extract_image(event: Any) -> Any | None:
    """从消息事件里取出图片组件。

    先看本条消息自带的附件；没有的话再看被引用消息——AstrBot 会把被回复消息
    的整条消息链放进 ``Reply.chain``，所以"回复奖品截图再发命令"这条路是通的。
    """
    getter = getattr(event, "get_messages", None)
    components = list(getter()) if callable(getter) else []
    if not components:
        message_obj = getattr(event, "message_obj", None)
        components = list(getattr(message_obj, "message", None) or [])

    found = _first_image(components)
    if found is not None:
        return found

    for component in components:
        chain = getattr(component, "chain", None)
        if chain:
            found = _first_image(chain)
            if found is not None:
                return found
    return None


async def fetch_bytes(component: Any) -> bytes:
    """读取图片组件的真实字节。

    走 AstrBot 的 ``convert_to_base64``（内部 ``MediaResolver`` 会按需下载网络
    图片），拿不到时退回 ``convert_to_file_path``。
    """
    to_base64 = getattr(component, "convert_to_base64", None)
    if callable(to_base64):
        raw = await to_base64()
        if isinstance(raw, bytes):
            return raw
        return base64.b64decode(raw)

    to_path = getattr(component, "convert_to_file_path", None)
    if callable(to_path):
        return Path(await to_path()).read_bytes()
    raise ValueError("无法读取图片内容")


def from_url(url: str) -> Any:
    """把 http(s) 链接包装成图片组件，由 AstrBot 负责下载。"""
    from .notifier import _components

    mc = _components()
    if mc is None:
        raise ValueError("消息组件不可用，无法按链接取图")
    return mc.Image.fromURL(url)


async def store_from_event(
    event: Any,
    data_dir: str | Path,
    group_id: str,
    seq: int,
) -> str | None:
    """事件里带图就存下来，返回相对路径；没带图返回 ``None``。"""
    component = extract_image(event)
    if component is None:
        return None
    data = await fetch_bytes(component)
    return save_poster(data_dir, group_id, seq, data)


async def store_from_url(
    url: str,
    data_dir: str | Path,
    group_id: str,
    seq: int,
) -> str:
    """下载链接里的图片并保存，返回相对路径。"""
    return save_poster(data_dir, group_id, seq, await fetch_bytes(from_url(url)))
