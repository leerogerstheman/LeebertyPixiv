#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ugoira.py —— pixiv 动图（うごイラ / Ugoira）的下载与转码

## pixiv 的 ugoira 到底是什么

它**不是**视频，也不是 GIF，而是一个 **ZIP 包**，里面装的是：

    xxx_ugoira1920x1080.zip
    ├── 000000.jpg      静态帧，第 1 帧
    ├── 000001.jpg      静态帧，第 2 帧
    ├── …
    └── （帧顺序与每帧停留时间由接口的 frames 字段单独给出）

也就是说：解压出来只是"一堆连续画面"，**本身没有动画**。要能播放，必须用每帧的
delay 把帧重新合成，这一步就叫「转码」。

## 转成什么

| 格式 | 特点 |
| --- | --- |
| `webp` | 推荐：1600 万色 + 支持透明，体积通常只有 GIF 的 1/3~1/5 |
| `gif`  | 最通用，但只有 256 色（渐变会出色带）；本模块用两级调色板尽量救画质 |
| `mp4`  | H.264，体积小、兼容好，但**不支持透明** |
| `webm` | VP9，体积最小，同样不支持透明 |

转码依赖外部程序 **ffmpeg**（本模块不打包它，也不要求必须安装）：
没有 ffmpeg 时，程序会如实告知并退化为"只保留原始 zip"，不会静默失败。

独立自检：`python ugoira.py` 会用合成数据验证时长/帧数计算是否正确（不需要 ffmpeg）。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 支持的目标格式 -> ffmpeg 参数（webp 单独处理，因为必须用 libwebp_anim）
FORMAT_ARGS: Dict[str, List[str]] = {
    "gif": ["-an"],          # GIF 走两级调色板，参数在命令里单独拼
    "mp4": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium",
            "-movflags", "+faststart", "-an"],
    "webm": ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-pix_fmt", "yuv420p",
             "-row-mt", "1", "-an"],
}
SUPPORTED_FORMATS = ("webp", "gif", "mp4", "webm")
MAX_OUTPUT_FRAMES = 200000      # 展开后的硬上限，防止异常数据把磁盘写爆
ZIP_EXT = ".zip"


class UgoiraError(Exception):
    pass


# --------------------------------------------------------------------------------------
# ffmpeg 探测
# --------------------------------------------------------------------------------------

def find_ffmpeg(configured: Optional[str] = None) -> Optional[str]:
    """找 ffmpeg：先看配置，再看 PATH，最后看程序目录下的 ffmpeg(.exe)。"""
    if configured:
        p = Path(configured).expanduser()
        if p.is_file():
            return str(p)
    found = shutil.which("ffmpeg")
    if found:
        return found
    for name in ("ffmpeg.exe", "ffmpeg"):
        local = Path(__file__).resolve().parent / name
        if local.is_file():
            return str(local)
    return None


def ffmpeg_hint() -> str:
    return ("未找到 ffmpeg，无法转码（已保留原始 zip）。安装方式：\n"
            "    Windows:  winget install Gyan.FFmpeg   或到 https://www.ffmpeg.org/download.html 下载后把\n"
            "              ffmpeg.exe 放到本程序目录，或把它的 bin 目录加入 PATH\n"
            "    macOS:    brew install ffmpeg\n"
            "    Ubuntu:   sudo apt install ffmpeg\n"
            "也可以在 config.json 里用 \"ffmpeg_path\" 指定完整路径。")


# --------------------------------------------------------------------------------------
# 帧时长 -> 帧率
# --------------------------------------------------------------------------------------

def choose_fps(delays: Sequence[int]) -> Tuple[float, int]:
    """把"每帧停留毫秒"换算成用于输出的固定帧率（返回 (fps, 最小时间单位 ms)）。

    原理：时长都是整数毫秒，可表示成 1/N 秒的整数倍。取所有 delay 的最大公约数 g，
    则最小时间单位 = g/1000 秒，帧率 1000/g 能**精确**还原每帧时长。
    例：delay 全 33ms -> g=33 -> 30.303 fps（不是粗暴按 30fps 播）。

    但**不能无限提高帧率**：实测踩过一个坑 —— 某动图 delay 是 10 的倍数（g=10），
    算出 100fps，于是 24 帧被展开成 300 帧，webp 产物从 4.8MB 膨胀到 81MB，播放还不对。
    所以这里加一个上限：不超过"平均帧率的 2 倍"。
    这样大多数动图（delay 均匀，如全 33ms）仍然是精确帧率，
    而 delay 比较碎的情况会退到略低但仍流畅的帧率，误差被摊到全程、肉眼看不出。
    """
    ds = [int(d) for d in delays if int(d) > 0]
    if not ds:
        return 24.0, 0
    g = ds[0]
    for d in ds[1:]:
        g = math.gcd(g, d)
    exact = 1000.0 / g
    avg = 1000.0 / (sum(ds) / len(ds))       # 平均帧率
    cap = avg * 2.0
    return min(exact, cap), g


def frames_per_second_hint(frames: Sequence[Dict[str, Any]]) -> Tuple[float, int]:
    """便于外部查看："精确帧率"与"最终采用帧率"分别是多少。"""
    ds = [int(f.get("delay") or 0) for f in frames]
    g = math.gcd(*[d for d in ds if d > 0]) if any(d > 0 for d in ds) else 0
    exact = 1000.0 / g if g else 24.0
    fps, _ = choose_fps(ds)
    return fps, g


def expand_frames(frames: Sequence[Dict[str, Any]], fps: float) -> List[str]:
    """按目标 fps 把每帧重复相应次数，得到逐帧文件序列。

    用"误差累积补偿"的四舍五入：把每帧的理想帧数与已分配的帧数之差带入下一帧，
    因此总帧数始终约等于 总时长×fps，不会像逐帧 floor 那样系统性偏长。
    """
    out: List[str] = []
    unit_ms = 1000.0 / fps
    allocated = 0.0
    for f in frames:
        name = str(f.get("file") or "")
        if not name:
            continue
        delay = max(1, int(f.get("delay") or 0))
        allocated += delay / unit_ms            # 到目前为止"应该"占用的帧数
        want = int(round(allocated))            # 累计取整，误差不累积
        repeat = max(1, want - len(out))
        if len(out) + repeat > MAX_OUTPUT_FRAMES:
            repeat = max(0, MAX_OUTPUT_FRAMES - len(out))
            if repeat == 0:
                break
        out.extend([name] * repeat)
    return out


def total_ms(frames: Sequence[Dict[str, Any]]) -> int:
    return sum(max(0, int(f.get("delay") or 0)) for f in frames)


# --------------------------------------------------------------------------------------
# 转码
# --------------------------------------------------------------------------------------

def convert_ugoira(zip_path: Path, frames: Sequence[Dict[str, Any]], out_format: str, *,
                   ffmpeg: Optional[str] = None, quality: Optional[int] = None,
                   timeout: int = 600, log=print) -> Tuple[Optional[Path], str]:
    """把 ugoira 的 zip 转成动图。返回 (输出文件路径 或 None, 说明文字)。"""
    out_format = (out_format or "").lower().strip()
    if out_format in ("", "none", "zip"):
        return None, "未启用转码"
    if out_format not in SUPPORTED_FORMATS:
        raise UgoiraError(f"不支持的 ugoira_format：{out_format!r}"
                          f"（可选 {', '.join(SUPPORTED_FORMATS)} 或 none）")
    exe = find_ffmpeg(ffmpeg)
    if not exe:
        return None, ffmpeg_hint()
    if not zip_path.is_file():
        raise UgoiraError(f"找不到 zip：{zip_path}")
    if not frames:
        raise UgoiraError("没有帧信息，无法转码")

    fps, g = choose_fps([int(f.get("delay") or 0) for f in frames])
    seq = expand_frames(frames, fps)
    if not seq:
        raise UgoiraError("展开帧序列为空")
    ms = total_ms(frames)

    out_path = zip_path.with_suffix("." + out_format)
    tmp_dir = Path(tempfile.mkdtemp(prefix="ugoira_"))
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
            for n in set(seq):
                if n in names:
                    z.extract(n, tmp_dir)
        # concat 清单：写 file + duration，并重复最后一行 ——
        # concat demuxer 的 duration 指令是"作用到下一个 file"，最后一帧会被忽略，
        # 官方推荐做法就是末尾再补一次同一张图（配合 -r 输出仍只算一次时长）。
        lines: List[str] = []
        for name in seq:
            if not (tmp_dir / name).is_file():
                continue
            lines.append(f"file '{name}'")
            lines.append(f"duration {1.0 / fps:.9f}")
        if not lines:
            raise UgoiraError("zip 里没有可用的帧文件")
        lines.append(f"file '{seq[-1]}'")
        list_file = tmp_dir / "frames.txt"
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        base = [exe, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_file)]
        if out_format == "gif":
            # 两级调色板：先统计全局调色板再抖动映射，明显好于默认 256 色
            vf = (f"fps={fps:.6f},scale=trunc(iw/2)*2:trunc(ih/2)*2,"
                  "split[a][b];[a]palettegen=stats_mode=diff[p];"
                  "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
            cmd = base + ["-vf", vf, "-loop", "0",
                          "-c:v", "gif", *( ["-q:v", str(quality)] if quality else []),
                          str(out_path)]
        elif out_format == "webp":
            # 动图 webp 必须用 libwebp_anim（普通 libwebp 只写单帧，会得到巨大的"动画"）
            vf = f"fps={fps:.6f},scale=trunc(iw/2)*2:trunc(ih/2)*2"
            cmd = base + ["-vf", vf, "-c:v", "libwebp_anim", "-lossless", "0",
                          "-q:v", str(quality or 75), "-compression_level", "6",
                          "-loop", "0", "-an", str(out_path)]
        else:
            vf = f"fps={fps:.6f},scale=trunc(iw/2)*2:trunc(ih/2)*2"
            cmd = base + ["-vf", vf] + list(FORMAT_ARGS[out_format]) + [str(out_path)]

        log(f"      转码 {out_format}：{len(seq)} 帧 @ {fps:.3f} fps"
            f"（共 {ms} ms，最小时间单位 {g} ms）")
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise UgoiraError("ffmpeg 失败：" + (" ｜ ".join(err[-3:]) if err else "未知错误"))
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise UgoiraError("ffmpeg 未产出有效文件")
        return out_path, f"{out_format} {out_path.stat().st_size/1024/1024:.2f} MB"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def extract_poster(zip_path: Path, dest: Path, frames: Sequence[Dict[str, Any]]) -> Optional[Path]:
    """把第一帧解出来作为静态封面，方便在 by_tag 等分类目录里直接预览。"""
    if not frames:
        return None
    first = str(frames[0].get("file") or "")
    if not first:
        return None
    try:
        with zipfile.ZipFile(zip_path) as z:
            if first not in z.namelist():
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(first) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return dest if dest.is_file() and dest.stat().st_size > 0 else None
    except (OSError, zipfile.BadZipFile):
        return None


# --------------------------------------------------------------------------------------
# 自检（不需要 ffmpeg）
# --------------------------------------------------------------------------------------

def _selftest() -> int:
    fails: List[str] = []

    def check(name: str, ok: bool, extra: Any = "") -> None:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
        if not ok:
            fails.append(name)

    print("ugoira 转码逻辑自检（不依赖 ffmpeg）")

    # 1) 均匀 33ms（实测最常见的 ugoira）
    fps, g = choose_fps([33] * 150)
    check("150 帧 × 33ms -> gcd=33", g == 33, f"g={g}")
    check("  fps ≈ 30.303", abs(fps - 1000 / 33) < 1e-9, f"{fps:.4f}")
    seq = expand_frames([{"file": f"{i:06d}.jpg", "delay": 33} for i in range(150)], fps)
    check("  展开后帧数保持 150", len(seq) == 150, len(seq))
    check("  总时长 4950ms 可被帧率整除", abs(len(seq) / fps * 1000 - 4950) < 0.5,
          f"{len(seq)/fps*1000:.2f} ms")

    # 2) 不均匀 delay（真实 ugoira 很常见）
    frames = [{"file": "a.jpg", "delay": 100}, {"file": "b.jpg", "delay": 200},
              {"file": "c.jpg", "delay": 100}]
    fps2, g2 = choose_fps([100, 200, 100])
    check("100/200/100 -> gcd=100 -> 10fps", g2 == 100 and abs(fps2 - 10.0) < 1e-9, f"g={g2} fps={fps2}")
    seq2 = expand_frames(frames, fps2)
    check("  展开为 1+2+1=4 帧", len(seq2) == 4, seq2)
    check("  总时长 400ms 精确", abs(len(seq2) / fps2 * 1000 - 400) < 0.5)
    check("  帧顺序正确", seq2 == ["a.jpg", "b.jpg", "b.jpg", "c.jpg"], seq2)

    # 3) 极小 delay：帧率精确跟随 gcd，总时长必须精确（不设帧率上限）
    frames3 = [{"file": f"{i}.jpg", "delay": 8} for i in range(50)]
    fps3, g3 = choose_fps([8] * 50)
    check("8ms -> fps = 125（精确）", abs(fps3 - 125.0) < 1e-9, f"{fps3}")
    seq3 = expand_frames(frames3, fps3)
    err = abs(len(seq3) / fps3 * 1000 - 400) / 400
    check("  总时长精确 (<0.01%)", err < 1e-4, f"误差 {err*100:.4f}%（{len(seq3)} 帧）")

    # 3b) 不均匀且含极小值：仍然不能出现大的时长漂移
    mix = [{"file": f"{i}.jpg", "delay": d} for i, d in enumerate([8, 33, 100, 8, 250, 33])]
    fps4, _ = choose_fps([f["delay"] for f in mix])
    seq4 = expand_frames(mix, fps4)
    want_ms = sum(f["delay"] for f in mix)
    got_ms = len(seq4) / fps4 * 1000
    check("混合 delay 总时长误差 < 2%", abs(got_ms - want_ms) / want_ms < 0.02,
          f"期望 {want_ms}ms 实得 {got_ms:.0f}ms（{len(seq4)} 帧）")

    # 4) 异常输入
    check("空 delay 列表有默认帧率", choose_fps([])[0] > 0)
    check("delay=0 不会死循环", len(expand_frames([{"file": "x.jpg", "delay": 0}], 10)) == 1)
    check("缺 file 字段被跳过", expand_frames([{"delay": 33}], 30) == [])
    check("total_ms 正确", total_ms(frames) == 400)

    # 5) 真实 zip 解析（用临时合成包，验证 extract_poster / 帧匹配）
    tmp = Path(tempfile.mkdtemp(prefix="ugoira_test_"))
    try:
        zp = tmp / "t.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("000000.jpg", b"\xff\xd8\xff\xe0FAKEJPEG" + b"\x00" * 32)
            z.writestr("000001.jpg", b"\xff\xd8\xff\xe0FAKEJPEG2" + b"\x00" * 32)
        poster = extract_poster(zp, tmp / "poster.jpg", [{"file": "000000.jpg", "delay": 33}])
        check("extract_poster 产出第一帧", poster is not None and poster.is_file())
        check("  内容非空", poster is not None and poster.stat().st_size > 10)
        check("无效帧名返回 None", extract_poster(zp, tmp / "x.jpg", [{"file": "nope.jpg"}]) is None)
        bad = tmp / "bad.zip"
        bad.write_bytes(b"not a zip")
        check("坏 zip 不抛异常", extract_poster(bad, tmp / "y.jpg", [{"file": "a.jpg"}]) is None)
        # 无 ffmpeg 时 convert 应给出友好提示而不是崩
        out, note = convert_ugoira(zp, [{"file": "000000.jpg", "delay": 33}], "webp",
                                   ffmpeg="__definitely_missing__")
        check("缺 ffmpeg 时返回 None + 提示", out is None and "ffmpeg" in note, note[:60])
        out2, note2 = convert_ugoira(zp, [{"file": "000000.jpg", "delay": 33}], "none")
        check("format=none 直接不转", out2 is None and "未启用" in note2)
        try:
            convert_ugoira(zp, [{"file": "a.jpg", "delay": 33}], "bogus")
            check("非法格式应报错", False, "未报错")
        except UgoiraError:
            check("非法格式应报错", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(_selftest())
