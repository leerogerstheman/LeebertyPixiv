#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pixiv 关键词爬虫 + 原图下载 + 可检索分类库
================================================

用法（命令行）:
    python pixiv_crawler.py crawl 初音ミク                 # 关键词搜索并下载原图
    python pixiv_crawler.py crawl 初音ミク --pages 3        # 前 3 页
    python pixiv_crawler.py crawl 初音ミク 风景 --pages 2   # 多个关键词
    python pixiv_crawler.py                          # 不带关键词 -> 进入交互式输入模式
    python pixiv_crawler.py search 初音                  # 在已下载的库里检索
    python pixiv_crawler.py stats                        # 库统计
    python pixiv_crawler.py reindex                      # 重建索引

设计要点:
  * 只用 Python 标准库（urllib / json / sqlite3），免 pip 安装。
  * 搜索走 pixiv 网页版 ajax 接口，**免登录**即可用；配置里填 PHPSESSID 后
    可看到更多内容（含 R-18）；填 refresh_token 则改用 App API（游标翻页更稳）。
  * 原图地址取自 /ajax/illust/{id}（及 /pages 取多图），带 Referer 头下载。
  * 分类法：原件统一存放在 _originals/，再按「标签」「画师」建立硬链接
    （同一磁盘不占额外空间），于是同一张图可被多个分类同时检索到。
  * 索引：_index/records.jsonl(主) + index.csv + index.md + catalog.sqlite(FTS5)
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import hashlib
import base64
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


def _load_sibling_module(filename: str, mod_name: str):
    """按文件路径加载同目录的辅助模块。

    两个必须注意的点：
      1. 直接 `import xxx` 在"以编程方式加载本文件"（sys.path 里没有脚本目录）时会失败，
         所以按文件路径加载；
      2. **必须先注册进 sys.modules 再 exec_module**：否则模块里用 @dataclass 时，
         dataclasses 内部 `sys.modules.get(cls.__module__)` 取到 None 而抛
         AttributeError（这个坑实测踩过）。
    """
    import importlib.util

    # frozen（exe）时以 exe 所在目录为准；否则是脚本所在目录
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    # PyInstaller onedir：辅助文件打包进 <exe 旁>/_internal；也在这里找
    for cand in (base, base / "_internal"):
        path = cand / filename
        if path.is_file():
            break
    else:
        return None
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:  # noqa: BLE001
        out(f"[warn] 加载 {filename} 失败：{type(exc).__name__}: {exc}")
        sys.modules.pop(mod_name, None)
        return None


ugoira_mod = _load_sibling_module("ugoira.py", "pixiv_ugoira")
artists_mod = _load_sibling_module("artists.py", "pixiv_artists")

__version__ = "1.0.0"

IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

# 接口端点。这些是"公认会随 pixiv 改版而变"的东西，所以支持从 config.json 覆盖：
#   在 config.json 里写 `"endpoints": {"search": "…", ...}` 即可替换，无需改代码。
# {kw}/{iid}/{uid} 是占位符，代码里会替换。
SEARCH_URL = "https://www.pixiv.net/ajax/search/artworks/{kw}"
ILLUST_URL = "https://www.pixiv.net/ajax/illust/{iid}"
PAGES_URL = "https://www.pixiv.net/ajax/illust/{iid}/pages"
UGOIRA_META_URL = "https://www.pixiv.net/ajax/illust/{iid}/ugoira_meta"
USER_ALL_URL = "https://www.pixiv.net/ajax/user/{uid}/profile/all"
USER_ILLUSTS_URL = "https://www.pixiv.net/ajax/user/{uid}/profile/illusts"
USER_BOOKMARKS_URL = "https://www.pixiv.net/ajax/user/{uid}/illusts/bookmarks"
WHOAMI_URL = "https://www.pixiv.net/ajax/user/self"
APP_SEARCH_URL = "https://app-api.pixiv.net/v1/search/illust"
APP_DETAIL_URL = "https://app-api.pixiv.net/v1/illust/detail"
APP_BOOKMARKS_URL = "https://app-api.pixiv.net/v1/user/bookmarks/illust"
APP_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"

ENDPOINT_DEFAULTS: Dict[str, str] = {
    "search": SEARCH_URL, "illust": ILLUST_URL, "pages": PAGES_URL,
    "ugoira_meta": UGOIRA_META_URL, "user_all": USER_ALL_URL,
    "user_illusts": USER_ILLUSTS_URL, "user_bookmarks": USER_BOOKMARKS_URL,
    "whoami": WHOAMI_URL,
    "app_search": APP_SEARCH_URL, "app_detail": APP_DETAIL_URL,
    "app_bookmarks": APP_BOOKMARKS_URL, "app_token": APP_TOKEN_URL,
}


def endpoint_url(cfg: Dict[str, Any], name: str, **fmt: str) -> str:
    """取接口 URL：config.json 的 endpoints.<name> 优先，否则用内置默认。"""
    conf = cfg.get("endpoints") or {}
    tpl = str(conf.get(name) or ENDPOINT_DEFAULTS.get(name) or "")
    if not tpl:
        return tpl
    try:
        return tpl.format(**fmt) if fmt else tpl
    except (KeyError, IndexError):
        return tpl
APP_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
APP_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
APP_HASH_SALT = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"

PIXIV_HOME = "https://www.pixiv.net/"
PIXIMG_HOME = "https://i.pximg.net/"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
APP_UA = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"

ORDER_MAP = {
    "date": "date_d",
    "date_d": "date_d",
    "popular": "popular_d",
    "popular_d": "popular_d",
    "popular_male": "popular_male_d",
    "popular_female": "popular_female_d",
    "old": "date",
    "date_a": "date",
}
MODE_MAP = {"all": "all", "safe": "safe", "r18": "r18"}
S_MODE_MAP = {"tag": "s_tag", "s_tag": "s_tag", "tag_full": "s_tag_full", "full": "s_tag_full", "text": "s_tc", "tc": "s_tc"}

TYPE_NAMES = {0: "illust", 1: "manga", 2: "ugoira"}

ILLEGAL_CHARS = '<>:"/\\|?*'
DEFAULT_TAG_STOPWORDS = [
    "オリジナル", "original", "イラスト", "illustration", "落書き", "らくがき",
    "練習", "sketch", "ラフ", "アナログ", "デジタル", "AI", "AIイラスト",
    "R-18", "R-18G", "R18", "R18G", "健全", "全年齢", "エロ",
    "漫画", "マンガ", "manga", "小説", "novel",
]
USERS_IRI_RE = re.compile(r"^\d*\s*(users|ユーザー|users入り|users入りタグ)", re.IGNORECASE)
USERS_IRI_RE2 = re.compile(r"users入り", re.IGNORECASE)
# 分类目录里的链接文件名 ..._<作品ID>_p<页码>.<ext>
LINK_NAME_RE = re.compile(r"_(\d{4,})_p(\d+)\.[A-Za-z0-9]+$")

# --------------------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------------------


class CrawlError(Exception):
    pass


class HttpError(CrawlError):
    def __init__(self, status: int, url: str, body: bytes = b"") -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body


class RateLimited(CrawlError):
    """被 pixiv 限流。

    单独一个异常类型，是因为它**绝不能被当成"该条件没有结果"**：
    限流时接口返回的是 HTTP 200 + total=0 + 空列表，与"这个时间段确实没作品"
    完全一样。搞混会导致数据被永久跳过（分段爬取时尤其致命）。
    """


class ApiShapeError(CrawlError):
    """接口返回结构不符合预期 —— 几乎一定是 pixiv 改版了。

    单独一个异常类型，是因为这类错误**必须响亮地报出来**：
    如果当成"没有结果"处理，爬虫就会静默失效（跑了一整批却一张都不下，用户以为
    "这关键词没人画"）。改成抛这个异常后，会带着实际返回的键名，明确告诉用户
    「接口可能改版了」，而不是假装正常。
    """


def _shape_msg(what: str, want: Any, got: Any) -> str:
    """把"接口结构不对"说得可操作：缺了哪些键、实际有些什么键。"""
    if isinstance(got, dict):
        actual = sorted(str(k) for k in got)[:12]
        summary = f"实际键：{actual}"
    else:
        summary = f"实际类型：{type(got).__name__}"
    return (f"{what} 结构异常：期望 {want}，但 {summary}。"
            "这几乎一定是 pixiv 接口改版了，请检查（或更新程序/配置里的接口模板）。")


def out(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # 极端老终端
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", "replace"))
        sys.stdout.flush()


def native_path(p: "os.PathLike[str] | str") -> str:
    """Windows 下用扩展路径前缀绕开 260 字符限制。"""
    s = os.path.abspath(os.fspath(p))
    if IS_WINDOWS and not s.startswith("\\\\?\\"):
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
        return "\\\\?\\" + s
    return s


def parse_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on", "是")


def format_size(num: int) -> str:
    f = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.2f} {unit}"
        f /= 1024
    return f"{f:.2f} TB"


def display_width(text: Any) -> int:
    """字符串在等宽终端里占的列数（中文/全角算 2 列）。

    `f"{s:<26}"` 是按**字符数**填充的，中文只算 1 个字符却占 2 列，
    所以含中文的表格用它会整片错位（实测踩到）。要排版就得自己算显示宽度。
    """
    width = 0
    for ch in str(text):
        code = ord(ch)
        wide = (0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF
                or 0xAC00 <= code <= 0xD7A3 or 0xF900 <= code <= 0xFAFF
                or 0xFE30 <= code <= 0xFE6F or 0xFF00 <= code <= 0xFF60
                or 0xFFE0 <= code <= 0xFFE6 or 0x20000 <= code <= 0x3FFFD)
        width += 2 if wide else 1
    return width


def pad_display(text: Any, width: int, align: str = "left") -> str:
    """按显示宽度把字符串填到指定列宽（中文按 2 列算）。"""
    s = str(text)
    gap = max(0, width - display_width(s))
    return (" " * gap + s) if align == "right" else (s + " " * gap)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------
# 时间范围筛选（用户在界面上填的开始/结束时间）
# --------------------------------------------------------------------------------------

_LOCAL_TZ = datetime.now().astimezone().tzinfo
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
)
# 只给到年月（"2024-07" / "2024/07"）或只给年（"2024"）的情形由 parse_user_date 用正则处理


class DateRange:
    """作品的发布时间范围过滤器。

    结束时间是**包含**的：用户填 2024-12-31，当天的作品会被收下。
    只填日期时按本地时区的整天理解；也支持 "2024-12-31 18:30" 这样的精确到分钟。
    """

    def __init__(self, after: Optional[datetime] = None, before: Optional[datetime] = None,
                 *, after_date_only: bool = False, before_date_only: bool = False) -> None:
        self.after = after
        self.before = before
        if after_date_only and after is not None:
            self.after = after.astimezone(_LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        if before_date_only and before is not None:
            self.before = (before.astimezone(_LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
                           + timedelta(days=1))

    @property
    def active(self) -> bool:
        return self.after is not None or self.before is not None

    def describe(self) -> str:
        def fmt(d: Optional[datetime]) -> str:
            return d.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M") if d else "不限"

        return f"{fmt(self.after)} ~ {fmt(self.before)}"

    def contains(self, dt: Optional[datetime]) -> bool:
        if not self.active or dt is None:
            return True
        if self.after is not None and dt < self.after:
            return False
        if self.before is not None and dt >= self.before:
            return False
        return True

    def is_older_than_range(self, dt: Optional[datetime]) -> bool:
        """作品比范围还早（且排序是从新到旧）时，说明后面的只会更早，可以停止翻页。"""
        return self.after is not None and dt is not None and dt < self.after


def parse_user_date(text: Any, mode: str = "start") -> Optional[datetime]:
    """解析用户输入的日期。

    支持：2024-01-31 / 2024/01/31 / 2024.01.31 / 20240131 / "2024-01-31 18:30" /
          "2024年1月31日"，以及**只给到年月**（2024-07）或**只给年**（2024）。

    mode 决定"没给到的部分"怎么补，这样界面上的三级下拉才有合理语义：
      mode="start"：2024    -> 2024-01-01 00:00；2024-07 -> 2024-07-01 00:00
      mode="end"  ：2024    -> 2025-01-01 00:00（= 整年，配合"结束日含当天"的语义）
                    2024-07 -> 2024-08-01 00:00（= 整月）
    """
    if text is None:
        return None
    # 中文日期写法归一化："2015年7月15日" -> "2015-7-15"，"2015年7月" -> "2015-7"
    s = re.sub(r"\s+", " ", str(text).strip())
    s = s.replace("年", "-").replace("月", "-").replace("日", "")
    s = s.strip().rstrip("-").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            naive = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=_LOCAL_TZ)
    # 只给到年月：统一补零后再解析（"2024-7"、"2024年7月" 都能认）
    m_ym = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", s)
    if m_ym:
        year, month = int(m_ym.group(1)), int(m_ym.group(2))
        if 1 <= month <= 12:
            naive = datetime(year, month, 1)
            if mode == "end":
                # 结束时间选到某个月 => 理解为"整月"，即到该月月底为止
                naive = (naive + timedelta(days=32)).replace(day=1)
            return naive.replace(tzinfo=_LOCAL_TZ)
    # 只给到年：结束时间理解为"整年"
    if re.fullmatch(r"\d{4}", s):
        year = int(s)
        naive = datetime(year, 1, 1)
        if mode == "end":
            naive = datetime(year + 1, 1, 1)
        return naive.replace(tzinfo=_LOCAL_TZ)
    raise ValueError(f"无法识别的时间格式：{text!r}（建议写成 2024-01-31 或 2024-01-31 18:30）")


def parse_range(cfg: Dict[str, Any]) -> DateRange:
    """从配置里读出时间范围。格式不对时给出提示并退化为不过滤，而不是让程序崩掉。"""
    def _is_date_only(text: Any) -> bool:
        """只写到"日"（YYYY-MM-DD）时，按本地整天理解并让结束日包含当天。"""
        raw = str(text or "").strip()
        if not raw:
            return False
        return bool(re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", raw))

    try:
        after = parse_user_date(cfg.get("date_from"), mode="start")
        after_d = _is_date_only(cfg.get("date_from"))
    except ValueError as exc:
        out(f"[warn] 起始时间无效，已忽略时间筛选：{exc}")
        return DateRange()
    try:
        before = parse_user_date(cfg.get("date_to"), mode="end")
        before_d = _is_date_only(cfg.get("date_to"))
    except ValueError as exc:
        out(f"[warn] 结束时间无效，已忽略时间筛选：{exc}")
        return DateRange()
    rng = DateRange(after, before, after_date_only=after_d, before_date_only=before_d)
    if rng.after and rng.before and rng.after >= rng.before:
        out(f"[warn] 起始时间（{rng.describe()}）不早于结束时间，已忽略时间筛选")
        return DateRange()
    return rng


def parse_illust_datetime(value: Any) -> Optional[datetime]:
    """解析 pixiv 返回的时间字符串（web 是 +09:00，app 是 +00:00）。"""
    if not value:
        return None
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_LOCAL_TZ)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---- 时间段分段（全量爬取用）----

SEG_DATE_FMT = "%Y-%m-%d"
# pixiv 网页搜索的翻页硬上限：实测单一查询约第 104 页起返回空列表（约 6180 条）。
# 因此每段的目标是"接口报告总数 < 这个值"，留出安全余量。
SEG_PAGE_CAP = 6180
SEG_TARGET = 4800           # 每段希望低于这个条数（≈80 页 × 60 条）
SEG_PAGES_PER_SEG = 90      # 每段最多翻多少页；超过就继续二分
                            # （实测单查询约第 104 页起返回空列表，留些余量）
SEG_MIN_DAYS = 1            # 最小切到 1 天（不再细分，否则会无限递归）
SEG_EPOCH = date(2007, 9, 1)   # pixiv 最早的作品发布于 2007-09（实测 order=date 首条为 2007-09-13）

# 连续这么多页都是"被筛选条件排除的新作品"就先停下。
# 注意与"翻到接口尽头"完全不同：这只是该筛选条件在此关键词下命中率太低。
FILTERED_PAGE_PATIENCE = 30

# 全量模式开跑前先做一次预估，用多少次探测请求
SEG_ESTIMATE_REQUESTS = 40


def date_to_scd(d: date) -> str:
    """date -> scd/ecd 参数用的 "YYYY-MM-DD"。"""
    return d.strftime(SEG_DATE_FMT)


def parse_seg_date(text: Any) -> Optional[date]:
    """把配置里的日期串解析成 date（只取日期部分）。"""
    dt = parse_user_date(text)
    return dt.date() if dt else None


def seg_key(keyword: str, scd: str, ecd: str, order: str, s_mode: str, mode: str) -> str:
    """一个时间段的唯一标识（用于记录"这段是否已爬完"）。"""
    return f"{keyword}\x1f{scd}\x1f{ecd}\x1f{order}\x1f{s_mode}\x1f{mode}"


def split_span(start: date, end: date) -> Optional[Tuple[Tuple[date, date], Tuple[date, date]]]:
    """把一个时间段二分。已经小到 1 天则返回 None（不可再分）。

    为什么必须二分而不是"按月切"：把 745,201 条按时间铺开是极不均匀的 ——
    冷门年份整年可能不到 5000 条（一次就能取完），热门月份可能上万条（必须再切）。
    固定粒度要么切太碎（白费请求），要么切不够（漏数据）。二分能自动适配。
    """
    days = (end - start).days + 1
    if days <= SEG_MIN_DAYS:
        return None
    mid = start + timedelta(days=(days // 2) - 1)
    if mid >= end:
        mid = end - timedelta(days=1)
    if mid < start:
        return None
    return (start, mid), (mid + timedelta(days=1), end)


# ---- 全量爬取的预估（数量 / 体积 / 耗时）----

DEFAULT_WORK_MB = 5.5          # 每作品平均体积的兜底值；库里有样本时用实测值
DEFAULT_THROUGHPUT = 1.5       # 单流有效下载速度 MB/s（保守估计）
# 实测：scd/ecd 区间的 total 在**跨度 ≤1 年**时完全可信（父子相加误差 0.0%），
# 但跨多年时父段的 total 会被截断（实测 +35% ~ +113% 的误差）。
# 所以预估/统计必须在"年内粒度"上求和，不能直接信一个多年区间的 total。
SEG_SAFE_SPAN_DAYS = 365


def measure_local_work_bytes(lib: "Library", *, min_works: int = 30) -> Optional[float]:
    """从已有图库算出「平均每个作品多少字节」，作为预估依据。

    用实测值而不是硬编码：不同关键词的题材差异很大（分辨率、页数都不同）。
    """
    try:
        recs = lib.load_records()
    except Exception:  # noqa: BLE001
        return None
    per_work: Dict[str, int] = {}
    for r in recs:
        iid = str(r.get("id") or "")
        if iid:
            per_work[iid] = per_work.get(iid, 0) + as_int(r.get("bytes"), 0)
    if len(per_work) < min_works:
        return None
    total = sum(per_work.values())
    return total / len(per_work) if per_work else None


def year_chunks(start: date, end: date) -> List[Tuple[date, date]]:
    """把日期范围切成跨度不超过 SEG_SAFE_SPAN_DAYS 的小块（按自然年）。

    为什么必须切：实测多年区间的 total 会被截断（父段小于子段之和），
    而年内区间的 total 精确（误差 0.0%）。所以统计总量要在年内粒度上求和。
    """
    out_chunks: List[Tuple[date, date]] = []
    cur = start
    while cur <= end:
        year_end = date(cur.year, 12, 31)
        chunk_end = min(year_end, end)
        out_chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return out_chunks


def count_works_in_range(
    cfg: Dict[str, Any],
    *,
    keyword: str,
    start: date,
    end: date,
    order: str = "date",
    mode: str = "all",
    s_mode: str = "s_tag",
    log: Optional[Any] = None,
) -> Dict[str, Any]:
    """在年内粒度上统计一个时间范围的作品总数（可信版本）。

    每个自然年各请求一次，累加各年的 total —— 因为跨多年的 total 不可信。
    """
    client = PixivClient(HttpClient(cfg, verbose=False), cfg, verbose=False)
    chunks = year_chunks(start, end)
    per_year: Dict[str, int] = {}
    total = 0
    requests = 0
    for a, b in chunks:
        try:
            _items, extra = client.search_illusts(keyword, 1, order=order, mode=mode,
                                                  s_mode=s_mode, scd=date_to_scd(a),
                                                  ecd=date_to_scd(b))
        except RateLimited:
            continue
        except CrawlError:
            continue
        n = as_int(extra.get("total"), 0)
        per_year[str(a.year)] = n
        total += n
        requests += 1
        if log:
            log(f"    {a.year} 年：{n} 个")
    return {"total": total, "per_year": per_year, "requests": requests,
            "years": len(chunks)}


# ---- 数量级表达（给"预估"用：用户要的是"几十 MB / 几小时"这种判断，不是精确值）----

_CN_DIGITS = "零一二三四五六七八九"


def cn_number(n: int) -> str:
    """整数 -> 中文数字（支持到亿级）。用于"四十几 GB"这种自然表达。"""
    n = int(n)
    if n < 0:
        return "负" + cn_number(-n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    if n < 100:
        return _CN_DIGITS[n // 10] + "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    if n < 1000:
        return (_CN_DIGITS[n // 100] + "百"
                + ("零" + cn_number(n % 100) if 0 < n % 100 < 10 else cn_number(n % 100) if n % 100 else ""))
    if n < 10000:
        return (_CN_DIGITS[n // 1000] + "千"
                + ("零" + cn_number(n % 1000) if 0 < n % 1000 < 100 else cn_number(n % 1000) if n % 1000 else ""))
    if n < 100000000:
        return cn_number(n // 10000) + "万" + (cn_number(n % 10000) if n % 10000 else "")
    return cn_number(n // 100000000) + "亿" + (cn_number(n % 100000000) if n % 100000000 else "")


# 数量级档位：(上限, 中文前缀)。用中文数字，避免"4十 GB"这种别扭写法。
_MAG_STEPS: List[Tuple[float, str]] = [
    (10, ""),          # 几
    (20, "十几"),
    (50, "几十"),      # 用"不到五十"表达 20~50 更自然
    (100, "一百"),
    (200, "两百"),
    (500, "几百"),
    (1000, "一千"),
    (2000, "两千"),
    (5000, "几千"),
    (10000, "一万"),
]


def magnitude_cn(value: float, unit: str = "") -> str:
    """把一个数字说成中文数量级：几 / 十几 / 几十 / 几百 / 几万 …

    用户看预估时关心的是"1 个 G 还是 1 个 T"，不是"2,308,891,234 字节"。
    数量级表达比精确数字更有用，也不会给人"估得很准"的错觉。
    """
    v = abs(float(value))
    if v <= 0:
        return "0" + unit
    if v < 1:
        return "不到 1" + unit

    def fmt(x: float) -> str:
        # 拉丁单位（MB/GB/TB）前留空格，中文单位（秒/分钟/小时/个）不留
        sp = " " if unit and unit[0].isascii() else ""
        if x < 10:
            return f"{cn_number(int(x))}{sp}{unit}"
        if x < 20:
            return f"十几{sp}{unit}"
        if x < 50:
            return f"不到{cn_number(int((x // 10 + 1) * 10))}{sp}{unit}"
        if x < 100:
            return f"{cn_number(int(x // 10))}十{sp}{unit}"
        if x < 1000:
            return f"{cn_number(int(x // 100))}百{sp}{unit}"
        if x < 10000:
            return f"{cn_number(int(x // 1000))}千{sp}{unit}"
        if x < 100000000:
            y = x / 10000
            if y < 10:
                return f"{cn_number(int(y))}万{sp}{unit}"
            if y < 50:
                return f"不到{cn_number(int((y // 10 + 1) * 10))}万{sp}{unit}"
            if y < 100:
                return f"{cn_number(int(y // 10))}十万{sp}{unit}"
            if y < 1000:
                return f"{cn_number(int(y // 100))}百万{sp}{unit}"
            return f"{cn_number(int(y // 1000))}千万{sp}{unit}"
        y = x / 100000000
        if y < 10:
            return f"{cn_number(int(y))}亿{sp}{unit}"
        return f"{cn_number(int(y // 10))}十亿{sp}{unit}"

    return fmt(v)


def magnitude_bytes(num_bytes: float) -> str:
    """字节数 -> 中文数量级（几十 MB / 几百 GB / 几 TB）。"""
    b = float(num_bytes)
    if b <= 0:
        return "0"
    if b < 1048576:
        return magnitude_cn(b / 1024, "KB")
    if b < 1073741824:
        return magnitude_cn(b / 1048576, "MB")
    if b < 1099511627776:
        return magnitude_cn(b / 1073741824, "GB")
    return magnitude_cn(b / 1099511627776, "TB")


def magnitude_seconds(sec: float) -> str:
    """秒数 -> 中文数量级（几分钟 / 几小时 / 几天）。"""
    s = float(sec)
    if s <= 0:
        return "几乎不需要时间"
    if s < 60:
        return magnitude_cn(s, "秒")
    if s < 3600:
        return magnitude_cn(s / 60, "分钟")
    if s < 86400:
        return magnitude_cn(s / 3600, "小时")
    return magnitude_cn(s / 86400, "天")


def estimate_summary(est: Dict[str, Any], *, disk_free: int = 0) -> List[str]:
    """把 estimate_crawl 的结果组织成给用户看的几行文字（数量级口径）。"""
    works = as_int(est.get("works"), 0)
    lines = [
        f"预计作品数：约 {magnitude_cn(works, '个')}"
        + (f"（{works:,}）" if works else ""),
        f"预计磁盘占用：{magnitude_bytes(est.get('disk_bytes', 0))}",
        f"预计总耗时：{magnitude_seconds(est.get('total_seconds', 0))}"
        f"（搜索 {magnitude_seconds(est.get('search_seconds', 0))}"
        f" + 下载 {magnitude_seconds(est.get('download_seconds', 0))}）",
    ]
    if est.get("segments"):
        lines.append(f"需要分成约 {est['segments']} 个时间段来爬"
                     + (f"，共约 {est['pages']:,} 次请求" if est.get("pages") else ""))
    if est.get("truncated_segments"):
        lines.append(f"⚠ 其中约 {est['truncated_segments']} 段属于热门时段，"
                     "单段超过接口上限，这部分可能取不全")
    if disk_free:
        need = as_int(est.get("disk_bytes"), 0)
        if need > disk_free:
            lines.append(f"⚠ 磁盘空间不足：需要 {magnitude_bytes(need)}，"
                         f"当前磁盘剩余 {magnitude_bytes(disk_free)}")
        else:
            lines.append(f"磁盘空间够用（剩余 {magnitude_bytes(disk_free)}）")
    if est.get("sampled"):
        lines.append("（采样估算：为节省请求只探测了一部分时间段，再按比例放大，"
                     "量级可靠、精确值不可靠）")
    if not works:
        lines.append("（没能探测到作品：可能是关键词无结果、被限流，或网络异常）")
    return lines


def estimate_crawl(
    cfg: Dict[str, Any],
    *,
    keyword: str,
    start: Optional[date] = None,
    end: Optional[date] = None,
    order: str = "date",
    mode: str = "all",
    s_mode: str = "s_tag",
    target: int = SEG_TARGET,
    pages_per_seg: int = SEG_PAGES_PER_SEG,
    max_requests: int = 120,
    work_bytes: Optional[float] = None,
    concurrency: int = 0,
    throughput_mb: float = DEFAULT_THROUGHPUT,
    need_detail: bool = False,
    log: Optional[Any] = None,
) -> Dict[str, Any]:
    """预估「把某个关键词全部爬下来」需要多少请求、多少磁盘、多长时间。

    为什么不能精确算：精确值需要把整棵分段树走一遍（每节点 1 次请求），
    而 74 万条量级的树有上千个节点 —— 那是几十分钟，不是"预估"。
    所以这里做**有界采样**：最多发 max_requests 次请求，把已探明部分按天数
    等比放大到全期，给出量级正确的估算，并明确标注这是估算。
    """
    def say(msg: str) -> None:
        if log:
            log(msg)

    client = PixivClient(HttpClient(cfg, verbose=False), cfg, verbose=False)
    start = start or SEG_EPOCH
    end = end or datetime.now().date()
    if start > end:
        start, end = end, start
    total_days = (end - start).days + 1

    res: Dict[str, Any] = {
        "keyword": keyword, "start": date_to_scd(start), "end": date_to_scd(end),
        "total_days": total_days, "requests_used": 0, "sampled": False,
        "reported_total": None, "hit_page_cap": False,
        "works": 0, "segments": 0, "pages": 0, "truncated_segments": 0,
        "search_seconds": 0.0, "disk_bytes": 0, "download_seconds": 0.0,
        "work_bytes": work_bytes or DEFAULT_WORK_MB * 1048576,
        "throughput_mb": throughput_mb, "concurrency": 0, "need_detail": need_detail,
    }

    # 有界 DFS：优先把"还没探明的天数"补齐，探满预算就停
    leaves: List[Tuple[int, int]] = []      # (天数, 条数)
    nodes_seen = 0
    root_last_page = 0
    # 记录"某个节点"的 total 与它两个子节点的 total，用于交叉校验 total 是否可信。
    # 实测 total 在宽区间下会退化（报成"到 ecd 为止的累计量"）：
    # 父段 68397，而两个子段是 24154 + 68397 —— 父小于子，数学上不可能。
    # 但窄区间下 total 是准的，所以用"子之和 ≈ 父"来判断这一层能不能信。
    tree_totals: Dict[int, Dict[str, int]] = {}

    def walk(a: date, b: date, depth: int = 0, node_id: int = 0) -> None:
        nonlocal nodes_seen, root_last_page
        if res["requests_used"] >= max_requests:
            return
        try:
            items, extra = client.search_illusts(
                keyword, 1, order=order, mode=mode, s_mode=s_mode,
                scd=date_to_scd(a), ecd=date_to_scd(b))
        except RateLimited:
            return
        except CrawlError as exc:
            say(f"  请求失败，提前结束预估：{exc}")
            return
        res["requests_used"] += 1
        nodes_seen += 1
        if res["reported_total"] is None:
            res["reported_total"] = as_int(extra.get("total"), 0) or None
        last_page = as_int(extra.get("last_page"), 0)
        if depth == 0:
            root_last_page = last_page
        if last_page >= 1000:
            res["hit_page_cap"] = True
        est_pages = last_page or 1
        page_limit = min(pages_per_seg, max(1, (target + 59) // 60))
        sub = split_span(a, b)
        node_total = as_int(extra.get("total"), 0)
        rec = tree_totals.setdefault(node_id, {})
        rec["total"] = node_total
        rec["days"] = (b - a).days + 1
        if est_pages > page_limit and sub is not None:
            walk(sub[0][0], sub[0][1], depth + 1, node_id * 2 + 1)
            walk(sub[1][0], sub[1][1], depth + 1, node_id * 2 + 2)
        else:
            # 叶节点：条数用 lastPage 推（total 在宽区间下不可靠）
            n = max(est_pages * 60, len(items))
            leaves.append(((b - a).days + 1, max(n, len(items))))
            if est_pages > SEG_PAGES_PER_SEG:
                res["truncated_segments"] += 1

    say(f"① 采样探测（最多 {max_requests} 次请求）…")
    walk(start, end)
    say(f"   已探测 {nodes_seen} 个节点，得到 {len(leaves)} 个可完整取到的区段")

    covered_days = sum(d for d, _ in leaves)
    covered_works = sum(n for _, n in leaves)
    res["sampled"] = covered_days < total_days
    if covered_days and covered_works:
        # 按天数等比放大到全期（结果在时间上不均匀，所以这是量级估算）
        density = covered_works / covered_days
        works = int(density * total_days)
        segs = len(leaves) * (total_days / covered_days)
    else:
        works, segs = 0, 0

    # 交叉校验：找最浅的一层，其"子段 total 之和 ≈ 父段 total"（允许 ±10%），
    # 说明这一层的 total 是可信的，就用子段之和作为更可靠的总量估计。
    # 这样能修正"有界 DFS 偏向老时段、按天数放大导致低估"的系统性偏差。
    checked = None
    for nid in sorted(tree_totals):
        left, right = tree_totals.get(nid * 2 + 1), tree_totals.get(nid * 2 + 2)
        if not left or not right:
            continue
        parent = tree_totals[nid].get("total", 0)
        s = left.get("total", 0) + right.get("total", 0)
        if parent and s and abs(s - parent) <= max(50, parent * 0.1):
            covered = left.get("days", 0) + right.get("days", 0)
            checked = {"level_total": s, "covered_days": covered, "parent_total": parent}
            break
    if checked and checked["covered_days"]:
        # 用可信层的密度外推到全期，比叶片密度更接近真实（该层跨度大得多）
        works2 = int(checked["level_total"] / checked["covered_days"] * total_days)
        if works2 > works:
            res["works_source"] = "层级 total 校验"
            works = works2
            segs = max(segs, works / max(1, target))
    if res["reported_total"]:
        # total 在宽区间下会小于真实值，取它作为下界参考
        res["works_vs_total"] = round(works / res["reported_total"], 2) if res["reported_total"] else 0
    res["works"] = works
    res["segments"] = int(round(segs))
    res["covered_days"] = covered_days
    res["covered_works"] = covered_works
    res["density_per_day"] = round(covered_works / covered_days, 1) if covered_days else 0

    # 页请求数：works/60 页，但撞上翻页上限的段拿不到全部
    res["pages"] = int(works / 60 + res["segments"])     # 每段 1 次"探页码"请求
    if need_detail:
        res["pages"] += works                            # 人气/AI 门槛需要逐作品取详情
    rps = float(cfg.get("requests_per_second") or 1.2)
    res["search_seconds"] = res["pages"] / rps if rps > 0 else 0.0
    # 搜索阶段的翻页还会撞上限（每段约 90 页有效），实际请求数已含在内
    res["disk_bytes"] = int(works * res["work_bytes"])
    conc = concurrency or max(1, int(cfg.get("concurrency") or 4))
    res["concurrency"] = conc
    mb = res["disk_bytes"] / 1048576.0
    eff = max(0.05, throughput_mb) * conc
    res["download_seconds"] = mb / eff if eff > 0 else 0.0
    res["total_seconds"] = res["search_seconds"] + res["download_seconds"]
    say(f"② 估算完成：约 {works} 个作品，{res['segments']} 段，"
        f"{res['pages']} 次请求，{format_size(res['disk_bytes'])}")
    return res





def kw_key(keyword: str) -> str:
    """关键词 -> 文件名安全片段（中文保留）。"""
    return sanitize_component(keyword, 48, fallback="keyword") or "keyword"


def sanitize_component(text: str, max_len: int = 80, fallback: str = "") -> str:
    """把任意标签/标题/画师名清洗成合法的单层目录或文件名片段。"""
    if text is None:
        raise ValueError("arg must not be None")
    s = unicodedata.normalize("NFC", str(text))
    # 去掉控制字符、换行、emoji 变体选择符也保留（Windows 允许）
    s = "".join(ch for ch in s if ch >= " " and ch != "\x7f")
    for ch in ILLEGAL_CHARS:
        s = s.replace(ch, "_")
    s = s.replace("\u3000", " ")
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Windows 不允许结尾的点或空格
    s = s.rstrip(" .")
    if len(s) > max_len:
        digest = hashlib.sha1(s.encode("utf-8")).hexdigest()[:6]
        s = s[: max_len - 7].rstrip(" .") + "_" + digest
    if not s:
        s = fallback or "unnamed"
    upper = s.upper()
    if upper.startswith("."):
        s = "_" + s[1:]
        upper = s.upper()
    if upper.split(".")[0] in {"CON", "PRN", "AUX", "NUL"} or re.match(r"^(COM|LPT)[1-9]$", upper):
        s = "_" + s
    return s


def clean_title(title: str, max_len: int = 60) -> str:
    s = sanitize_component(title, max_len, fallback="untitled")
    return s or "untitled"


def dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    res = []
    for x in items:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


def ensure_dir(p: "os.PathLike[str] | str") -> None:
    os.makedirs(native_path(p), exist_ok=True)


def file_size(p: "os.PathLike[str] | str") -> int:
    try:
        return os.path.getsize(native_path(p))
    except OSError:
        return 0


def link_or_copy(src: Path, dst: Path) -> str:
    """优先硬链接（同盘零拷贝），失败则复制。返回 'hardlink'/'copy'/'exists'。"""
    if os.path.exists(native_path(dst)):
        return "exists"
    ensure_dir(dst.parent)
    try:
        os.link(native_path(src), native_path(dst))
        return "hardlink"
    except OSError:
        try:
            shutil.copy2(native_path(src), native_path(dst))
            return "copy"
        except OSError as exc:
            raise CrawlError(f"建立分类链接失败: {dst} ({exc})") from exc


def guess_ext(url: str, default: str = ".jpg") -> str:
    path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".bmp", ".svg"):
        return ".jpg" if ext == ".jpeg" else ext
    return default


def atomic_write_json(p: Path, obj: Any) -> None:
    ensure_dir(p.parent)
    tmp = p.with_name(p.name + ".tmp")
    with open(native_path(tmp), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(native_path(tmp), native_path(p))


def read_json(p: Path, default: Any = None) -> Any:
    try:
        with open(native_path(p), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------------------


def default_config(program_dir: Path) -> Dict[str, Any]:
    return {
        "output_dir": str(program_dir / "library"),
        "cookie_phpsessid": "",
        "refresh_token": "",
        "proxy": "",
        "verify_ssl": True,
        "requests_per_second": 1.2,
        "max_retries": 5,
        "timeout": 30,
        "concurrency": 4,
        "search_delay": 0.35,
        "pages": 1,
        "order": "date",
        "mode": "all",
        "s_mode": "s_tag",
        "search_type": "all",
        # 0 = 不限制（有多少收多少），避免默认值悄悄截断结果
        "max_works_per_run": 0,
        "skip_r18": True,
        "skip_r18g": True,
        "skip_ugoira": True,
        # 动图（ugoira）：pixiv 给的是"帧 + 每帧时长"的 zip，需要 ffmpeg 才能转成能播放的动图
        "ugoira_format": "none",        # none / webp / gif / mp4 / webm
        "ugoira_keep_zip": True,        # 转码后是否保留原始 zip（动图 zip 通常几十 MB）
        "ffmpeg_path": "",              # 留空=自动查找 PATH 与程序目录
        # 时间范围：留空=不限制；支持 2024-01-31 或 "2024-01-31 18:30"
        "date_from": "",
        "date_to": "",
        # 人气门槛：0=不限制。注意这两个数只能从作品详情取得
        "min_likes": 0,
        "min_bookmarks": 0,
        "max_pages_per_work": 0,
        "min_tag_count": 1,
        "max_tags_per_work": 8,
        "tag_stopwords": DEFAULT_TAG_STOPWORDS,
        "make_tag_links": True,
        "make_author_links": True,
        "make_keyword_links": True,
        "user_agent": DESKTOP_UA,
    }


CONFIG_TEMPLATE = {
    "_说明": "config.json —— 改完保存即可，命令行参数会覆盖这里的同名项",
    "output_dir": "D:\\PixivCrawler\\library",
    "cookie_phpsessid": "（可选）登录 pixiv 后从浏览器 Cookie 里复制 PHPSESSID 的值，可搜索到更多内容",
    "refresh_token": "（可选，二选一）pixiv OAuth refresh_token，填了就改用 App API 搜索",
    "proxy": "（可选）例如 http://127.0.0.1:7890",
    "verify_ssl": True,
    "requests_per_second": 1.2,
    "max_retries": 5,
    "timeout": 30,
    "concurrency": 4,
    "pages": 1,
    "order": "date",
    "mode": "all",
    "s_mode": "s_tag",
    "max_works_per_run": 500,
    "skip_r18": True,
    "skip_ugoira": True,
    "min_tag_count": 1,
    "max_tags_per_work": 8,
    "make_tag_links": True,
    "make_author_links": True,
    "make_keyword_links": True,
}


def find_config_path(program_dir: Path, explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        return Path(explicit).expanduser()
    for cand in (
        program_dir / "config.json",
        Path.cwd() / "config.json",
        Path.home() / ".pixiv_crawler" / "config.json",
    ):
        if os.path.isfile(native_path(cand)):
            return cand
    return None


def load_config(program_dir: Path, explicit: Optional[str]) -> Tuple[Dict[str, Any], Optional[Path]]:
    cfg = default_config(program_dir)
    path = find_config_path(program_dir, explicit)
    if path and os.path.isfile(native_path(path)):
        data = read_json(path, {})
        if isinstance(data, dict):
            for k, v in data.items():
                if k.startswith("_") or v is None:
                    continue
                if isinstance(v, str) and v.startswith(("（可选", "（说明")):
                    continue
                cfg[k] = v
    return cfg, path


# --------------------------------------------------------------------------------------
# HTTP 客户端（限速 + 重试 + 断点续传）
# --------------------------------------------------------------------------------------


class HttpClient:
    def __init__(self, cfg: Dict[str, Any], *, verbose: bool = True) -> None:
        self.cfg = cfg
        self.verbose = verbose
        self.ua = str(cfg.get("user_agent") or DESKTOP_UA)
        self.timeout = float(cfg.get("timeout") or 30)
        self.max_retries = int(cfg.get("max_retries") or 5)
        rps = float(cfg.get("requests_per_second") or 1.2)
        self.base_interval = 1.0 / rps if rps > 0 else 0.0
        self.min_interval = self.base_interval
        # 自适应限速：命中限流就把间隔翻倍（上限 30s），连续一段时间正常就慢慢收回来。
        # 全量爬取（上万次请求）必须有这个，否则跑几小时必被掐，而且被掐后会静默漏数据。
        self._limit_until = 0.0
        self._ok_streak = 0
        proxy = str(cfg.get("proxy") or "").strip()
        handlers: List[Any] = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        if not parse_bool(cfg.get("verify_ssl"), True):
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self._lock = threading.Lock()
        self._last_request = 0.0
        self.stats = {"requests": 0, "bytes": 0, "retries": 0, "errors": 0,
                      "rate_limited": 0, "total_wait": 0.0}

    # ---- 基础 ----

    def _throttle(self) -> None:
        if self.min_interval <= 0 and self._limit_until <= 0:
            return
        with self._lock:
            now = time.monotonic()
            # 限流冷却优先：命中限流后强制睡够，避免继续触发
            if self._limit_until > now:
                wait = self._limit_until - now
                self.stats["total_wait"] += wait
                if self.verbose:
                    out(f"    [限流冷却] 等待 {wait:.0f}s（已自适应降速到 "
                        f"{self.min_interval:.1f}s/请求）")
                time.sleep(wait)
                now = time.monotonic()
            delta = now - self._last_request
            wait = self.min_interval - delta
            if wait > 0:
                self.stats["total_wait"] += wait
                time.sleep(wait)
            self._last_request = time.monotonic()

    def note_rate_limited(self, cooldown: float = 0.0) -> float:
        """记录一次"疑似限流"：拉长请求间隔并进入冷却。返回本次冷却秒数。"""
        with self._lock:
            self.stats["rate_limited"] += 1
            n = self.stats["rate_limited"]
            self.min_interval = min(30.0, max(self.base_interval, 0.5) * (2 ** min(n, 6)))
            cool = cooldown or min(600.0, 20.0 * (2 ** min(n - 1, 5)))
            self._limit_until = time.monotonic() + cool
            self._ok_streak = 0
            return cool

    def note_ok(self) -> None:
        """连续顺利的请求足够多时，把间隔慢慢收回基线（避免永久处于慢速）。"""
        with self._lock:
            self._ok_streak += 1
            if self._ok_streak >= 50 and self.min_interval > self.base_interval:
                self.min_interval = max(self.base_interval, self.min_interval / 2.0)
                self._ok_streak = 0

    def in_cooldown(self) -> bool:
        return self._limit_until > time.monotonic()

    def _headers(self, extra: Optional[Dict[str, str]], referer: str) -> Dict[str, str]:
        h = {
            "User-Agent": self.ua,
            "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
            "Referer": referer,
        }
        if extra:
            h.update(extra)
        return h

    def _open(self, req: urllib.request.Request, *, stream: bool):
        return self.opener.open(req, timeout=self.timeout)

    def request(
        self,
        url: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        referer: str = PIXIV_HOME,
        method: Optional[str] = None,
        allow_404: bool = False,
    ) -> Tuple[int, bytes]:
        """返回 (status, body)。allow_404 时 404 返回 (404, b"") 而不抛异常。"""
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=self._headers(headers, referer))
            try:
                with self._open(req, stream=False) as resp:
                    body = resp.read()
                    with self._lock:
                        self.stats["requests"] += 1
                        self.stats["bytes"] += len(body)
                    self.note_ok()
                    return int(resp.status), body
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                body = b""
                try:
                    body = exc.read(4096)
                except Exception:
                    pass
                try:
                    exc.close()
                except Exception:
                    pass
                if status == 404 and allow_404:
                    return 404, b""
                if status in (429, 500, 502, 503, 504):
                    wait = min(60.0, (2.0 ** attempt) + random.uniform(0, 1.5))
                    if status == 429:
                        # 429 是明确的限流信号：进入冷却并自适应降速，光重试没用
                        wait = max(wait, 10.0)
                        self.note_rate_limited(cooldown=wait)
                    self.stats["retries"] += 1
                    if attempt < self.max_retries:
                        if self.verbose:
                            out(f"    [warn] HTTP {status}，{wait:.1f}s 后重试 ({attempt}/{self.max_retries})")
                        time.sleep(wait)
                        last_exc = HttpError(status, url, body)
                        continue
                self.stats["errors"] += 1
                raise HttpError(status, url, body)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.stats["retries"] += 1
                last_exc = exc
                if attempt < self.max_retries:
                    wait = min(30.0, (2.0 ** attempt) + random.uniform(0, 1.0))
                    if self.verbose:
                        out(f"    [warn] 网络异常 {type(exc).__name__}，{wait:.1f}s 后重试 ({attempt}/{self.max_retries})")
                    time.sleep(wait)
                    continue
                self.stats["errors"] += 1
                raise CrawlError(f"网络请求失败: {url} ({exc})") from exc
        raise CrawlError(f"重试 {self.max_retries} 次仍失败: {url} ({last_exc})")

    def get_json(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        referer: str = PIXIV_HOME,
        allow_404: bool = False,
    ) -> Any:
        status, body = self.request(url, headers=headers, referer=referer, allow_404=allow_404)
        if status == 404:
            return None
        text = body.decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise CrawlError(f"接口返回不是合法 JSON: {url} -> {text[:200]!r}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise CrawlError(f"接口报错: {url} -> {str(payload.get('message'))[:200]}")
        return payload

    # ---- 下载 ----

    def download(self, url: str, dest: Path, *, referer: str = PIXIV_HOME) -> Tuple[bool, str]:
        """下载到 dest。返回 (是否成功, 说明)。支持断点续传。"""
        dest = Path(dest)
        if os.path.exists(native_path(dest)) and file_size(dest) > 0:
            return True, "exists"
        ensure_dir(dest.parent)
        part = dest.with_name(dest.name + ".part")
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self.max_retries + 1):
            offset = file_size(part)
            headers = self._headers(None, referer)
            mode = "wb"
            if offset > 0:
                headers["Range"] = f"bytes={offset}-"
                mode = "ab"
            self._throttle()
            req = urllib.request.Request(url, headers=headers)
            try:
                with self._open(req, stream=True) as resp:
                    status = int(resp.status)
                    if status == 200 and offset > 0:
                        mode, offset = "wb", 0  # 服务端不支持 Range，重下
                    if status not in (200, 206):
                        raise HttpError(status, url)
                    with open(native_path(part), mode) as fh:
                        while True:
                            chunk = resp.read(262144)
                            if not chunk:
                                break
                            fh.write(chunk)
                    final = file_size(part)
                    with self._lock:
                        self.stats["requests"] += 1
                        self.stats["bytes"] += final - offset
                if final <= 0:
                    raise CrawlError("下载得到 0 字节")
                os.replace(native_path(part), native_path(dest))
                return True, "ok"
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                try:
                    exc.close()
                except Exception:
                    pass
                last_exc = HttpError(status, url)
                if status == 404:
                    return False, "404 原图不存在"
                if status in (403, 401):
                    if attempt >= 2:
                        return False, f"{status} 被拒绝（可能需要登录/作品已限制）"
                if status == 429 or status >= 500:
                    wait = max(10.0, 2.0 ** attempt)
                    if self.verbose:
                        out(f"    [warn] 图片 HTTP {status}，{wait:.1f}s 后重试 ({attempt}/{self.max_retries})")
                    time.sleep(wait)
                    continue
                return False, f"HTTP {status}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if isinstance(exc, OSError) and not isinstance(exc, (urllib.error.URLError, TimeoutError)):
                    raise CrawlError(f"写入文件失败: {dest} ({exc})") from exc
                if attempt < self.max_retries:
                    time.sleep(min(30.0, 2.0 ** attempt))
                    continue
                break
            except CrawlError as exc:
                last_exc = exc
                break
        self.stats["errors"] += 1
        try:
            if file_size(part) == 0:
                os.remove(native_path(part))
        except OSError:
            pass
        return False, f"失败: {last_exc}"


# --------------------------------------------------------------------------------------
# pixiv 业务客户端
# --------------------------------------------------------------------------------------


class PixivClient:
    def __init__(self, http: HttpClient, cfg: Dict[str, Any], *, verbose: bool = True,
                 anonymous: bool = False) -> None:
        """anonymous=True 时**忽略凭据**，强制匿名 —— 用于对比"匿名 vs 登录"的可爬量。"""
        self.http = http
        self.cfg = cfg
        self.verbose = verbose
        if anonymous:
            # 强行匿名：清空凭据后再走正常初始化，保证与"真匿名用户"完全同路径
            cfg = dict(cfg)
            cfg["cookie_phpsessid"] = ""
            cfg["refresh_token"] = ""
            self.cfg = cfg
        self.cookie = str(cfg.get("cookie_phpsessid") or "").strip()
        self.refresh_token = str(cfg.get("refresh_token") or "").strip()
        self.access_token = ""
        self.user_id = ""
        self.logged_in = False
        self.rotated_token = False
        self._empty_streak = 0        # 连续"空结果"次数，用于软限流识别
        self._lock = threading.Lock()
        if self.refresh_token:
            self._login_with_token()

    # ---- 认证 ----

    def _auth_headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if self.cookie:
            h["Cookie"] = f"PHPSESSID={self.cookie}"
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def _login_with_token(self) -> None:
        import uuid

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        client_hash = hashlib.md5((ts + APP_HASH_SALT).encode("utf-8")).hexdigest()
        headers = {
            "User-Agent": APP_UA,
            "App-OS": "android",
            "App-OS-Version": "11",
            "App-Version": "5.0.234",
            "X-Client-Time": ts,
            "X-Client-Hash": client_hash,
            # 必须带客户端标识头：只放在 body 里会被 pixiv 拒为
            # invalid_client（实测 "Client id was not found in the headers or body"）
            "X-Client-Id": APP_CLIENT_ID,
            "X-Client-Secret": APP_CLIENT_SECRET,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        body = urllib.parse.urlencode(
            {
                "get_secure_url": "1",
                "client_id": APP_CLIENT_ID,
                "client_secret": APP_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "device_token": uuid.uuid4().hex,
            }
        ).encode("utf-8")
        try:
            _, raw = self.http.request(endpoint_url(self.cfg, "app_token"), data=body, headers=headers, method="POST", referer="")
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            out(f"[warn] refresh_token 登录失败（忽略，继续匿名模式）: {exc}")
            return
        self.access_token = str(data.get("access_token") or "")
        self.user_id = str((data.get("user") or {}).get("id") or "")
        # pixiv 每次刷新都会轮换 refresh_token；旧的不再有效，必须把新的存回去，
        # 否则用户下次运行就会因为用旧 token 而登录失败。
        new_token = str(data.get("refresh_token") or "")
        if new_token and new_token != self.refresh_token:
            self.refresh_token = new_token
            try:
                save_credential_store(new_token, kind="refresh_token",
                                     user={"id": self.user_id} if self.user_id else None)
                self.rotated_token = True
            except OSError as exc:
                out(f"[warn] 新的 refresh_token 未能保存（不影响本次运行）：{exc}")
        if self.access_token:
            self.logged_in = True
            out(f"[i] 已用 refresh_token 登录 pixiv App API（user_id={self.user_id or '未知'}）")
        else:
            out("[warn] refresh_token 未换到 access_token，继续匿名模式")

    def preflight(self) -> None:
        """确认当前凭据可用性（失败不致命）。"""
        if self.access_token:
            return
        try:
            payload = self.http.get_json(endpoint_url(self.cfg, "illust", iid="1"), headers=self._auth_headers(), allow_404=True)
            if payload is None:
                out("[warn] 连接 pixiv 时检测到作品 404（可能被墙/需要代理），继续尝试")
        except CrawlError as exc:
            out(f"[warn] 预检失败: {exc}")

    def verify_login(self) -> Optional[Dict[str, Any]]:
        """确认 cookie / token 是否**真的**处于登录状态。

        只检查 cookie 非空是不够的：失效的 cookie 会被 pixiv 当作匿名处理，
        程序会静默退回匿名模式，让人误以为"已登录却爬得很少"。

        注意 pixiv 这个接口返回的是 {"userData": {...}}（不是 body），
        早期版本写成 body.id 会把有效登录态误判为失效 —— 已修正。
        """
        if self.access_token:
            return {"user_id": self.user_id, "name": "", "via": "refresh_token"}

        payload = self.http.get_json(endpoint_url(self.cfg, "whoami"), headers=self._auth_headers(), allow_404=True)
        if not isinstance(payload, dict):
            return None
        for holder_key in ("userData", "body"):
            holder = payload.get(holder_key)
            if isinstance(holder, dict) and holder.get("id"):
                return {
                    "user_id": str(holder.get("id")),
                    "name": str(holder.get("name") or ""),
                    "pixiv_id": str(holder.get("pixivId") or ""),
                    "premium": bool(holder.get("premium")),
                    "adult": bool(holder.get("adult")),
                    "x_restrict": holder.get("xRestrict"),
                    "via": "cookie",
                }
        # 有 error 字段 = 无效/过期（正常返回 None）；没有 userData/body/error = 结构变了
        if "error" not in payload:
            raise ApiShapeError(_shape_msg("登录态校验", "userData 或 error 字段", payload))
        return None

    def show_login_status(self) -> Optional[Dict[str, Any]]:
        """打印并返回登录状态（供自检与爬取启动时调用）。"""
        if self.access_token:
            out(f"  [OK]   已用 refresh_token 登录（user_id={self.user_id or '未知'}）")
            return {"user_id": self.user_id, "name": "", "via": "refresh_token"}
        if not self.cookie:
            out("  [WARN] 未配置登录凭据 —— 匿名模式：可搜索、可下原图，"
                "但每种排序只能拿到约 600 个作品")
            return None
        try:
            info = self.verify_login()
        except CrawlError as exc:
            out(f"  [WARN] 无法校验登录态：{exc}")
            return None
        if info:
            self.logged_in = True
            who = f"{info.get('name')}"
            if info.get("pixiv_id"):
                who += f"（@{info['pixiv_id']}）"
            who += f" id={info.get('user_id')}"
            out(f"  [OK]   登录态有效：{who}")
            extra = []
            if info.get("premium"):
                extra.append("premium")
            if info.get("adult"):
                extra.append("已开启成人内容")
            if extra:
                out(f"         账号属性：{'、'.join(extra)}")
            out("         搜索将使用登录权限，可获取的作品明显多于匿名模式")
            return info
        out("  [FAIL] 配置里的 PHPSESSID 已失效（pixiv 视为未登录）")
        out("         请重新获取：登录 pixiv → 复制新的 PHPSESSID → 填入 config.json 的 cookie_phpsessid")
        return None

    # ---- 搜索 ----

    def search_illusts(
        self,
        keyword: str,
        page: int,
        *,
        order: str = "date",
        mode: str = "all",
        s_mode: str = "s_tag",
        scd: str = "",
        ecd: str = "",
        allow_fallback: bool = True,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """返回 (作品简表列表, 附加信息)。

        scd/ecd：可选的服务端日期筛选（"YYYY-MM-DD"）。带上它们才能把大结果集
        按时间段切分，从而绕过单一搜索的翻页上限（详见 crawl_segmented）。

        allow_fallback：网页接口结构异常时，若已登录 refresh_token，自动切换到
        App API（官方接口，更稳）——实现"网页挂了还有第二引擎兜底"。
        """
        # 显式选择或已有 App token：直接用 App API（官方接口，最稳）
        if self.access_token:
            return self._search_app(keyword, order=order, mode=mode, s_mode=s_mode, page=page)
        try:
            res = self._search_web(keyword, page, order=order, mode=mode, s_mode=s_mode,
                                   scd=scd, ecd=ecd)
        except ApiShapeError:
            # 网页接口结构变了（几乎肯定是 pixiv 改版）。
            # 已经拿到 refresh_token 的会在 __init__ 阶段就进入 App API，走不到这里；
            # 走到的都是 cookie 模式 —— 没得兜底，必须把"响亮死"的消息抛出去。
            if not (allow_fallback and self.cfg.get("refresh_token")):
                raise
        # 软限流识别：被限流时接口返回 HTTP 200 + total=0 + 空列表，
        # 与"这个时间段确实没有作品"长得一模一样。连续空页就判定为限流，
        # 否则调用方会把它当成"该段爬完了"而**永久跳过这段数据**（实测踩到）。
        items, extra = res
        if not items and as_int(extra.get("total"), 0) == 0:
            with self._lock:
                self._empty_streak += 1
                streak = self._empty_streak
            if streak >= 2:
                cool = self.http.note_rate_limited()
                self._empty_streak = 0
                raise RateLimited(
                    f"连续 {streak} 次搜索返回 total=0（疑似限流），已冷却 {cool:.0f}s 并降速到 "
                    f"{self.http.min_interval:.1f}s/请求。若你确定该条件本就无结果，可加 --ignore-empty")
        else:
            with self._lock:
                self._empty_streak = 0 if items else self._empty_streak
        return res

    def _search_web(
        self, keyword: str, page: int, *, order: str, mode: str, s_mode: str,
        scd: str = "", ecd: str = "",
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        kw = urllib.parse.quote(keyword)
        params = {
            "word": keyword,
            "order": ORDER_MAP.get(order, "date_d"),
            "mode": MODE_MAP.get(mode, "all"),
            "p": str(max(1, page)),
            "s_mode": S_MODE_MAP.get(s_mode, "s_tag"),
            "type": str(self.cfg.get("search_type") or "all"),
            "lang": "zh",
        }
        # scd/ecd 是 pixiv 网页搜索的**服务端**日期筛选（实测确认生效）：
        # 带上它们，每个时间段都有独立的翻页额度，因而是"全量爬取"的关键。
        if scd:
            params["scd"] = scd
        if ecd:
            params["ecd"] = ecd
        url = endpoint_url(self.cfg, "search", kw=kw) + "?" + urllib.parse.urlencode(params)
        payload = self.http.get_json(url, headers=self._auth_headers())
        body = (payload or {}).get("body") or {}
        im = body.get("illustManga")
        if not isinstance(im, dict):
            # 结构校验：找不到 illustManga 十有八九是 pixiv 改版，要响亮地报出来
            raise ApiShapeError(_shape_msg(f"网页搜索（{keyword}）", "body.illustManga", body))
        items = im.get("data") or []
        extra = {
            "total": im.get("total"),
            "last_page": im.get("lastPage"),
            "related_tags": [t.get("tag") for t in (body.get("relatedTags") or []) if isinstance(t, dict)],
            "source": "web",
            "scd": scd,
            "ecd": ecd,
            "raw_keys": sorted(items[0].keys()) if items and isinstance(items[0], dict) else [],
        }
        return [x for x in items if isinstance(x, dict)], extra

    def _search_app(
        self, keyword: str, *, order: str, mode: str, s_mode: str, page: int
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """App API：按页取（内部仍用 next 游标，翻到指定页）。"""
        params = {
            "word": keyword,
            "search_target": {"s_tag": "partial_match_for_tags", "s_tag_full": "exact_match_for_tags",
                              "s_tc": "title_and_caption"}.get(s_mode, "partial_match_for_tags"),
            "sort": {"date_d": "date_desc", "date": "date_asc", "popular_d": "popular_desc"}.get(
                ORDER_MAP.get(order, "date_d"), "date_desc"
            ),
            "filter": "for_ios",
        }
        if MODE_MAP.get(mode) == "safe":
            params["search_target"] = params["search_target"]
        url = endpoint_url(self.cfg, "app_search") + "?" + urllib.parse.urlencode(params)
        items: List[Dict[str, Any]] = []
        next_url: Optional[str] = url
        current = 1
        while next_url and current <= max(1, page):
            data = self.http.get_json(
                next_url,
                headers={**self._auth_headers(), "User-Agent": APP_UA, "Accept": "application/json"},
            )
            if not isinstance(data, dict):
                break
            for it in data.get("illusts") or []:
                sid = str(it.get("id") or "")
                first = (it.get("image_urls") or {}).get("large") or ""
                items.append(
                    {
                        "id": sid,
                        "title": it.get("title") or "",
                        "userId": str(it.get("user", {}).get("id") or ""),
                        "userName": it.get("user", {}).get("name") or "",
                        "pageCount": it.get("page_count") or 1,
                        "xRestrict": it.get("x_restrict") or 0,
                        "illustType": it.get("type") or "illust",
                        "createDate": it.get("create_date") or "",
                        "tags": it.get("tags") or [],
                        "width": it.get("width"),
                        "height": it.get("height"),
                        "_first_original": app_url_to_original(first),
                    }
                )
            next_url = data.get("next_url")
            current += 1
            if self.verbose and next_url and current <= page:
                time.sleep(float(self.cfg.get("search_delay") or 0.3))
        return items, {"total": None, "related_tags": [], "source": "app"}

    # ---- 作品详情 ----

    def illust_detail(self, illust_id: str) -> Optional[Dict[str, Any]]:
        if self.access_token:
            try:
                payload = self.http.get_json(
                    endpoint_url(self.cfg, "app_detail") + "?" + urllib.parse.urlencode({"illust_id": illust_id}),
                    headers={**self._auth_headers(), "User-Agent": APP_UA},
                )
                body = (payload or {}).get("illust")
                if isinstance(body, dict):
                    body["_source"] = "app"
                    return body
            except CrawlError:
                pass
        payload = self.http.get_json(
            endpoint_url(self.cfg, "illust", iid=illust_id), headers=self._auth_headers(), allow_404=True
        )
        if payload is None:
            return None
        if not isinstance(payload, dict):
            return None
        body = payload.get("body")
        if isinstance(body, dict):
            body["_source"] = "web"
            return body
        # 带 error 字段的响应 = 作品已删除/受限（合法跳过，不是改版）
        if "error" in payload:
            return None
        # 没有 body、也没有 error —— 结构变了，几乎肯定是接口改版，要响亮地报
        raise ApiShapeError(_shape_msg(f"作品详情（{illust_id}）", "body 或 error 字段", payload))

    def illust_pages(self, illust_id: str, page_count: int) -> List[Dict[str, Any]]:
        """返回每页的 {url, ext, width, height}；失败时按首图 URL 规律推断。"""
        got: List[Dict[str, Any]] = []
        try:
            payload = self.http.get_json(endpoint_url(self.cfg, "pages", iid=illust_id), headers=self._auth_headers(), allow_404=True)
            arr = (payload or {}).get("body") if payload else None
            if isinstance(arr, list) and arr:
                for item in arr:
                    urls = (item or {}).get("urls") or {}
                    u = urls.get("original") or urls.get("regular") or urls.get("small") or ""
                    if u:
                        got.append(
                            {
                                "url": u,
                                "ext": guess_ext(u),
                                "width": item.get("width"),
                                "height": item.get("height"),
                            }
                        )
        except CrawlError:
            got = []
        if got:
            return got
        return got  # 由调用方回退到 urls.original

    def artist_work_ids(self, artist_id: str) -> Tuple[List[str], Dict[str, str]]:
        """取某画师的全部作品 ID（插画 + 漫画，按发布时间从新到旧）。

        实测确认的接口行为：
          * /ajax/user/{id}/profile/all 返回 {illusts:{作品ID:null}, manga:{...}}
            —— **只有 ID，没有时间/标签**，所以够不够新要靠"是否已下载"来判断
          * 该接口最多只给约 50 个 ID；要用 /ajax/user/{id}/profile/illusts?ids[]=… 补全
        返回 (有序 ID 列表, ID->作者名)。
        """
        url = endpoint_url(self.cfg, "user_all", uid=artist_id)
        payload = self.http.get_json(url, headers=self._auth_headers(), allow_404=True)
        body = (payload or {}).get("body") if payload else None
        if not isinstance(body, dict):
            return [], {}
        ids: List[str] = []
        for kind in ("illusts", "manga"):
            holder = body.get(kind)
            if isinstance(holder, dict):
                ids.extend(str(k) for k in holder.keys())
            elif isinstance(holder, list):
                ids.extend(str(x) for x in holder)
        ids = dedupe_keep_order(ids)
        if not ids:
            return [], {}

        # 取一次详情批量接口：能拿到标题/时间/页码，且顺带确认作品仍然存在
        meta: Dict[str, str] = {}
        author_name = ""
        chunk = 24
        for i in range(0, len(ids), chunk):
            batch = ids[i:i + chunk]
            q = "&".join(f"ids%5B%5D={x}" for x in batch)
            try:
                p2 = self.http.get_json(
                    f"{endpoint_url(self.cfg, "user_illusts", uid=artist_id)}?{q}&work_category=illustManga&is_first_page=0",
                    headers=self._auth_headers(), allow_404=True)
            except CrawlError:
                break
            works = ((p2 or {}).get("body") or {}).get("works") or {}
            if not isinstance(works, dict):
                break
            for wid, w in works.items():
                if isinstance(w, dict):
                    meta[str(wid)] = str(w.get("createDate") or w.get("updateDate") or "")
                    if not author_name:
                        author_name = str(w.get("userName") or "")
            time.sleep(0.2)
        return ids, ({artist_id: author_name} if author_name else {})

    def ugoira_meta(self, illust_id: str) -> Optional[Dict[str, Any]]:
        """取 ugoira（动图）的真实地址与帧信息。

        ugoira 不能用 urls.original 取——那个字段指向的是缩略图路径，实测 404。
        正确来源是专用接口 /ajax/illust/{id}/ugoira_meta，返回：
            {src, originalSrc, mime_type, frames:[{file, delay}, ...]}
        """
        try:
            payload = self.http.get_json(endpoint_url(self.cfg, "ugoira_meta", iid=illust_id),
                                         headers=self._auth_headers(), allow_404=True)
        except CrawlError:
            return None
        if payload is None:
            return None
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            if isinstance(payload, dict) and "error" in payload:
                return None
            raise ApiShapeError(_shape_msg(f"动图元信息（{illust_id}）", "body", payload or {}))
        url = str(body.get("originalSrc") or body.get("src") or "")
        if not url:
            return None
        frames = body.get("frames")
        return {
            "url": url,
            "src": str(body.get("src") or ""),
            "mime_type": str(body.get("mime_type") or ""),
            "frames": frames if isinstance(frames, list) else [],
            "ext": guess_ext(url, ".zip"),
        }

    def collect_bookmarks(self, user_id: str, *, rest: str = "show",
                          max_works: int = 0, tag: str = "",
                          log=out) -> Tuple[List[Dict[str, Any]], int]:
        """拉取某用户收藏夹里的作品简表（网页书签接口，分页取全）。

        rest: "show"=公开收藏（任何人可见，网页接口即可）
              "hide"=私密收藏（仅本人 + 需要 refresh_token/App API）
        返回 (作品简表列表, 找到的作品总数)。简表字段与搜索列表一致
        （id/title/tags/aiType/pageCount 等），可在列表阶段筛选，不额外消耗请求。
        """
        briefs: List[Dict[str, Any]] = []
        total = 0
        offset = 0
        limit = 48
        # 私密收藏必须走 App API（网页接口不提供 hide）
        if rest == "hide":
            if not self.access_token:
                raise CrawlError("爬取私密收藏需要 refresh_token（App API 登录）。"
                                 "用 `auth login --method token` 登录后再试。")
            url = endpoint_url(self.cfg, "app_bookmarks")
            q = urllib.parse.urlencode({"user_id": str(user_id), "rest": "hide"})
            next_url = f"{url}?{q}"
            hdr = {**self._auth_headers(), "User-Agent": APP_UA}
            while next_url:
                data = self.http.get_json(next_url, headers=hdr)
                if not isinstance(data, dict):
                    break
                items = data.get("illusts") or []
                for it in items:
                    sid = str(it.get("id") or "")
                    if not sid:
                        continue
                    briefs.append({
                        "id": sid,
                        "title": it.get("title") or "",
                        "userId": str((it.get("user") or {}).get("id") or ""),
                        "userName": str((it.get("user") or {}).get("name") or ""),
                        "pageCount": it.get("page_count") or 1,
                        "xRestrict": it.get("x_restrict") or 0,
                        "illustType": it.get("type") or "illust",
                        "createDate": it.get("create_date") or "",
                        "tags": [{"tag": str(t)} if isinstance(t, str) else t
                                 for t in (it.get("tags") or [])],
                        "aiType": 1 if (it.get("ai_type") or 0) != 0 else 0,
                    })
                total = len(briefs)
                nxt = data.get("next_url")
                if nxt and (max_works <= 0 or len(briefs) < max_works):
                    next_url = nxt
                    if self.verbose:
                        time.sleep(float(self.cfg.get("search_delay") or 0.3))
                else:
                    break
            return briefs[:max_works] if max_works > 0 else briefs, total

        # ---- 公开收藏：网页接口 ----
        hdr = self._auth_headers()
        while True:
            params = {"tag": tag, "offset": offset, "limit": limit, "rest": "show"}
            url = endpoint_url(self.cfg, "user_bookmarks",
                               uid=str(user_id)) + "?" + urllib.parse.urlencode(params)
            payload = self.http.get_json(url, headers=hdr)
            body = (payload or {}).get("body") or {}
            items = body.get("works") or []
            if body.get("total") is None and not items:
                # total/tag 字段结构变了 —— 大概率改版
                if isinstance(body, dict) and "works" not in body:
                    raise ApiShapeError(_shape_msg(f"收藏夹（{user_id}）", "body.works",
                                                  payload or {}))
            total = int(body.get("total") or 0)
            for w in items:
                if not isinstance(w, dict):
                    continue
                sid = str(w.get("id") or "")
                if not sid:
                    continue
                briefs.append({
                    "id": sid,
                    "title": w.get("title") or "",
                    "userId": str(w.get("userId") or ""),
                    "userName": str(w.get("userName") or ""),
                    "pageCount": int(w.get("pageCount") or 1),
                    "xRestrict": int(w.get("xRestrict") or 0),
                    "illustType": w.get("illustType") or "illust",
                    "createDate": w.get("createDate") or "",
                    "tags": w.get("tags") or [],
                    "aiType": int(w.get("aiType") or 0) if w.get("aiType") is not None else 0,
                })
            if max_works > 0 and len(briefs) >= max_works:
                break
            if len(items) < limit or offset + len(items) >= total:
                break
            offset += len(items)
            if self.verbose:
                log(f"  [i] 收藏夹翻页：已取 {len(briefs)}/{total}")
                time.sleep(float(self.cfg.get("search_delay") or 0.3))
        if self.verbose:
            log(f"  [i] 收藏夹共 {total} 个作品，已取 {len(briefs)} 个")
        return briefs[:max_works] if max_works > 0 else briefs, total

    def resolve_original_urls(self, illust_id: str,
                              detail: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """解析作品所有页的原图地址。detail 可省略（会自动取一次详情）。

        注意：ugoira（动图）是**一个 zip**，不是多张图，必须走 ugoira_meta；
        用 urls.original 会拼出一个不存在的 ..._ugoira0_p0.jpg（实测 404）。
        """
        if detail is None:
            detail = self.illust_detail(illust_id) or {}
        itype = detail.get("illustType")
        is_ugoira = (itype == 2) or (isinstance(itype, str) and itype.lower() == "ugoira")
        if is_ugoira:
            meta = self.ugoira_meta(illust_id)
            if meta:
                return [{"url": meta["url"], "ext": meta["ext"], "width": detail.get("width"),
                         "height": detail.get("height"), "is_ugoira": True,
                         "frames": meta["frames"], "mime_type": meta["mime_type"]}]
            return []

        first = ((detail.get("urls") or {}).get("original")) or detail.get("_first_original") or ""
        if not first:
            first = app_url_to_original(((detail.get("meta_single_page") or {}).get("original_image_url")) or "")
        page_count = int(detail.get("pageCount") or detail.get("page_count") or 1)
        pages = self.illust_pages(illust_id, page_count) if page_count > 1 else []
        if pages:
            return pages
        if not first:
            return []
        base, ext = os.path.splitext(first)
        base = re.sub(r"_p\d+$", "", base)
        res = []
        for idx in range(max(1, page_count)):
            # 多图时优先试原扩展名，其次 .jpg/.png
            u = f"{base}_p{idx}{ext}"
            res.append({"url": u, "ext": ext or ".jpg", "width": None, "height": None})
        return res


def app_url_to_original(url: str) -> str:
    """App API 的 image_urls.large -> 原图 URL（按惯例把 c/... 段换成 img-original）。"""
    if not url:
        return ""
    return re.sub(r"/c/[\dA-Za-z_x]+/img-master/", "/img-original/", url).replace("_master1200", "")


# --------------------------------------------------------------------------------------
# 记录构造 / 分类
# --------------------------------------------------------------------------------------


def _split_tag(name: str) -> List[str]:
    """拆开 pixiv 偶发的拼接标签（如 '初音ミク,' / '初音ミク, VOCALOID'）。

    pixiv 的合法标签本身不含逗号/顿号，所以遇到这些分隔符就可以安全拆开；
    拆完去掉空片段和首尾空白。返回拆分后的标签名列表。
    """
    name = str(name).strip()
    if not name:
        return []
    parts = re.split(r"[,，、;；/]", name)
    return [p.strip() for p in parts if p.strip()]


def tags_from_detail(detail: Dict[str, Any], stopwords: Sequence[str], max_tags: int) -> List[Dict[str, str]]:
    """从作品详情里取出干净的标签列表（去掉 users入り 之类的噪声标签）。

    web 接口 tags.tags 是 [{tag, translation}]，app 接口是 ["tag", ...]，两者都支持。
    """
    raw_obj = detail.get("tags")
    if isinstance(raw_obj, dict):
        raw = raw_obj.get("tags") or []
    else:
        raw = raw_obj or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raw = []
    stop = {str(s).strip().lower() for s in stopwords}
    res: List[Dict[str, str]] = []
    for t in raw:
        if isinstance(t, dict):
            name = str(t.get("tag") or "").strip()
            trans = ""
            tr = t.get("translation") or {}
            if isinstance(tr, dict):
                trans = str(tr.get("zh") or tr.get("zh_tw") or tr.get("en") or "").strip()
        else:
            name, trans = str(t).strip(), ""
        # 拼接标签拆分（如 '初音ミク,' 拆成 '初音ミク'）
        for name in _split_tag(name):
            if not name:
                continue
            if name.lower() in stop:
                continue
            if USERS_IRI_RE.match(name) or USERS_IRI_RE2.search(name):
                continue
            res.append({"name": name, "translation": trans})
    seen = set()
    uniq: List[Dict[str, str]] = []
    for t in res:
        key = t["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(t)
    if max_tags > 0:
        uniq = uniq[:max_tags]
    return uniq


def build_record(
    *,
    illust_id: str,
    page_index: int,
    detail: Dict[str, Any],
    url: str,
    ext: str,
    path_rel: str,
    bytes_size: int,
    width: Optional[int],
    height: Optional[int],
    keyword: str,
    tags: List[Dict[str, str]],
    query_keys: List[str],
    rank: int,
    order: str,
) -> Dict[str, Any]:
    title = str(detail.get("title") or detail.get("illustTitle") or "")
    author = str(detail.get("userName") or "")
    author_id = str(detail.get("userId") or "")
    create = str(detail.get("createDate") or detail.get("uploadDate") or "")
    itype_raw = detail.get("illustType")
    itype = TYPE_NAMES.get(int(itype_raw), "illust") if isinstance(itype_raw, (int, str)) and str(itype_raw).isdigit() else (
        str(itype_raw) if isinstance(itype_raw, str) else "illust"
    )
    return {
        "id": str(illust_id),
        "page": page_index,
        "page_count": int(detail.get("pageCount") or detail.get("page_count") or 1),
        "title": title,
        "author": author,
        "author_id": author_id,
        "type": itype,
        "x_restrict": int(detail.get("xRestrict") or 0),
        "ai": int(detail.get("aiType") or 0),
        "width": width,
        "height": height,
        "bytes": bytes_size,
        "ext": ext,
        "file": path_rel.replace("\\", "/"),
        "url": url,
        "page_url": f"https://www.pixiv.net/artworks/{illust_id}",
        "tags": [t["name"] for t in tags],
        "tags_i18n": {t["name"]: t["translation"] for t in tags if t.get("translation")},
        "query": keyword,
        "query_keys": query_keys,
        "rank": rank,
        "order": order,
        "create_date": create,
        "bookmark_count": detail.get("bookmarkCount"),
        "like_count": detail.get("likeCount"),
        "view_count": detail.get("viewCount"),
        "crawled_at": now_iso(),
    }


def tag_dir_name(tag: str, max_len: int = 64) -> str:
    return sanitize_component(tag, max_len, fallback="tag")


def author_dir_name(author: str, author_id: str) -> str:
    name = sanitize_component(author or "unknown", 48, fallback="unknown")
    return f"{name}_{sanitize_component(author_id, 16, fallback='0')}" if author_id else name


def link_name(rec: Dict[str, Any], seq: int) -> str:
    author = sanitize_component(rec.get("author") or "unknown", 24, fallback="unknown")
    title = clean_title(rec.get("title") or "", 48)
    # 分类目录里链接的可能是封面帧或转码后的动图，扩展名要跟着实际链接目标走，
    # 否则会出现"文件名是 .zip、内容其实是 JPEG"这种误导
    ext = os.path.splitext(str(rec.get("file") or ""))[1] or rec.get("ext") or ".jpg"
    return f"{seq:04d}_{author}_{title}_{rec['id']}_p{rec.get('page', 0)}{ext}"


# --------------------------------------------------------------------------------------
# 爬取进度（断点续爬）
# --------------------------------------------------------------------------------------


class SessionState:
    """记录每个（关键词 + 搜索组合）的进度状态，支持中断后续爬、跳过已耗尽的组合。

    exhausted: 已经翻到接口尽头的组合，重跑时直接跳过，避免白发请求
    partial:   跑了但没标记完成的组合（多半是被中断），下次从第 1 页续爬（已下载的图会跳过）
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.exhausted: Set[str] = set()
        self.partial: Set[str] = set()
        self.incomplete_run: bool = False
        self.load()

    @staticmethod
    def make_key(keyword: str, order: str, s_mode: str) -> str:
        return f"{keyword}|{order}|{s_mode}"

    def load(self) -> None:
        data = read_json(self.path, {}) or {}
        self.exhausted = {str(k) for k in (data.get("exhausted") or [])}
        run = data.get("incomplete_run") or {}
        self.partial = {str(k) for k in (run.get("keys") or [])} if run else set()
        self.incomplete_run = bool(run)

    def save(self, run_keys: Sequence[str], *, run_done: bool) -> None:
        payload: Dict[str, Any] = {
            "updated_at": now_iso(),
            "exhausted": sorted(self.exhausted),
            "exhausted_count": len(self.exhausted),
        }
        if run_keys:
            payload["incomplete_run"] = {
                "started_at": now_iso(),
                "keys": sorted(run_keys),
                "note": "这次运行的组合尚未全部走完；下次运行会从这些组合继续",
            }
        atomic_write_json(self.path, payload)

    def mark_exhausted(self, key: str) -> None:
        self.exhausted.add(key)

    def reset(self) -> None:
        self.exhausted.clear()
        self.partial.clear()
        self.incomplete_run = False
        try:
            if os.path.exists(native_path(self.path)):
                os.remove(native_path(self.path))
        except OSError:
            pass


class SegmentStore:
    """记录「哪些时间段已经完整爬完」，用于全量分段爬取的断点续爬。

    为什么段级状态是必须的：全量爬取要跑上万次请求、几十小时。
    如果只记录"组合级"状态，中断后重跑就得把整棵分段树重走一遍，
    已经爬完的段会白白再请求一次（每段至少 1 次请求，几万次就是几小时）。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.done: Set[str] = set()
        self.stats: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        data = read_json(self.path, {}) or {}
        self.done = {str(k) for k in (data.get("done") or [])}
        self.stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}

    def save(self, extra: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {
            "updated_at": now_iso(),
            "done_count": len(self.done),
            "done": sorted(self.done),
        }
        if self.stats or extra:
            payload["stats"] = {**self.stats, **(extra or {})}
        atomic_write_json(self.path, payload)

    def is_done(self, key: str) -> bool:
        return key in self.done

    def mark_done(self, key: str, *, count: int = 0) -> None:
        self.done.add(key)
        if count:
            self.stats[key] = count

    def reset(self) -> None:
        self.done.clear()
        self.stats.clear()
        try:
            if os.path.exists(native_path(self.path)):
                os.remove(native_path(self.path))
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# 认证 / 凭据管理
#
# 凭据来源优先级（后者覆盖前者）：
#     内置 config.json  ->  用户凭据库 ~/.pixiv_crawler/credentials.json
#       ->  环境变量 PIXIV_REFRESH_TOKEN / PIXIV_PHPSESSID  ->  命令行参数
#
# 推荐用 refresh_token（官方 App API 的 OAuth 凭据，配合 PKCE 由 pixiv 签发）；
# PHPSESSID 作为备选，适合不想走 OAuth 的用户。
# --------------------------------------------------------------------------------------

CRED_DIR = Path.home() / ".pixiv_crawler"
CRED_FILE = CRED_DIR / "credentials.json"
ENV_REFRESH_TOKEN = "PIXIV_REFRESH_TOKEN"
ENV_PHPSESSID = "PIXIV_PHPSESSID"
AUTHORIZE_URL = "https://app-api.pixiv.net/web/v1/login"
OAUTH_REDIRECT = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"


def secure_write(path: Path, text: str) -> None:
    """写入敏感文件并尽量限制权限（Windows 依赖用户目录 ACL；类 Unix 设为 0600）。"""
    ensure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    with open(native_path(tmp), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(native_path(tmp), native_path(path))
    if os.name != "nt":
        try:
            os.chmod(native_path(path), 0o600)
        except OSError:
            pass


def load_credential_store() -> Dict[str, Any]:
    data = read_json(CRED_FILE, {})
    return data if isinstance(data, dict) else {}


def save_credential_store(token: str, *, kind: str = "refresh_token",
                          user: Optional[Dict[str, Any]] = None,
                          extra: Optional[Dict[str, Any]] = None) -> Path:
    """把凭据写入用户凭据库（不写进项目里的 config.json，避免被误提交到 git）。"""
    store = load_credential_store()
    store["refresh_token" if kind == "refresh_token" else "phpsessid"] = token
    if user:
        store["user"] = user
    store["updated_at"] = now_iso()
    if extra:
        store.update(extra)
    secure_write(CRED_FILE, json.dumps(store, ensure_ascii=False, indent=2) + "\n")
    return CRED_FILE


def clear_credential_store() -> bool:
    try:
        if os.path.exists(native_path(CRED_FILE)):
            os.remove(native_path(CRED_FILE))
            return True
    except OSError:
        pass
    return False


# --------------------------------------------------------------------------------------
# 读取能力体检（CLI `auth doctor`、图形界面「读取能力」页、自检 共用同一份实现）
# --------------------------------------------------------------------------------------

PROBE_KEYWORD = "初音ミク"      # 用一个人气很高的词才能测出"上限"，冷门词两边都少
ANON_CAP = 600                  # 实测：匿名每种排序约 598 个就被截断
PROBE_MAX_PAGES = 24            # 探测时的翻页上限，避免体检本身太慢


def probe_capability(
    cfg: Dict[str, Any],
    *,
    client: Optional["PixivClient"] = None,
    keyword: str = PROBE_KEYWORD,
    max_pages: int = PROBE_MAX_PAGES,
    check_download: bool = True,
    skip_anon: bool = False,
    log: Optional[Any] = None,
    progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """体检「当前配置到底能读取多少内容」，并给出可执行的修复指引。

    为什么要真探测而不是只看配置：cookie 可能过期、账号可能被限流、
    代理可能只放通部分接口 —— 这些情况下"看起来配了登录"但实际仍按匿名返回。
    所以这里实际发请求，用**同一关键词下登录/匿名的可爬量对比**作为判据。

    返回结构（GUI 直接渲染它）：
        verdict      : "full" | "limited" | "anonymous" | "error"
        cred         : 凭据来源、cookie 长度、凭据库路径与时间
        login        : 登录态（昵称/ID/premium/adult）
        capacity     : 登录与匿名各自能翻到多少、倍数、是否触顶
        download     : 能否取到原图地址并真正下载
        checks       : [(名称, 状态, 说明)]，状态 ∈ ok/warn/bad
        advice       : [给用户的下一步操作建议]
    """
    def say(msg: str) -> None:
        if log:
            log(msg)

    def tick(msg: str) -> None:
        """进度上报：界面上要能看到「转到哪一步了」，
        否则 40 次请求要几十秒，界面只有一句「正在检测…」，看起来就像卡死了。"""
        if progress:
            progress(msg)

    res: Dict[str, Any] = {
        "keyword": keyword,
        "cred": {}, "login": None, "capacity": {}, "download": {},
        "checks": [], "advice": [], "verdict": "error", "error": "",
    }
    checks = res["checks"]

    # ---- ① 凭据来源 ----
    store = load_credential_store()
    cookie = str(cfg.get("cookie_phpsessid") or "").strip()
    token = str(cfg.get("refresh_token") or "").strip()
    res["cred"] = {
        "source": credentials_source(cfg),
        "store_path": str(CRED_FILE),
        "store_exists": os.path.isfile(native_path(CRED_FILE)),
        "store_updated": str(store.get("updated_at") or ""),
        "store_has_token": bool(store.get("refresh_token")),
        "store_has_sess": bool(store.get("phpsessid")),
        "config_path": str(cfg.get("_config_path") or ""),
        "has_cookie": bool(cookie),
        "cookie_len": len(cookie),
        "has_token": bool(token),
        "proxy": str(cfg.get("proxy") or ""),
    }
    if token:
        checks.append(("凭据", "ok", "已配置 refresh_token（App 接口，最稳）"))
    elif cookie:
        if len(cookie) < 20:
            checks.append(("凭据", "warn", f"PHPSESSID 只有 {len(cookie)} 字符，正常是 41 字符，可能没复制完整"))
        else:
            checks.append(("凭据", "ok", f"已配置 PHPSESSID（{len(cookie)} 字符）"))
    else:
        checks.append(("凭据", "bad", "没有任何登录凭据 → 只能匿名读取"))
        res["advice"].append("点「从浏览器自动导入」或「手动填 PHPSESSID」来登录")

    try:
        own = client or PixivClient(HttpClient(cfg, verbose=False), cfg, verbose=False)
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"初始化客户端失败：{exc}"
        checks.append(("网络", "bad", res["error"]))
        return res

    # ---- ② 登录态 ----
    say("① 校验登录态…")
    try:
        info = own.verify_login()
    except CrawlError as exc:
        res["error"] = str(exc)
        checks.append(("登录态", "bad", f"请求失败：{exc}"))
        res["advice"].append("检查网络或代理设置（config.json 的 proxy）")
        return res
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
        checks.append(("登录态", "bad", res["error"]))
        return res

    if info:
        res["login"] = info
        name = info.get("name") or "（名字未知）"
        pid = info.get("pixiv_id") or info.get("user_id") or "?"
        extra = []
        if info.get("premium"):
            extra.append("会员")
        extra.append("已开启成人内容" if info.get("adult") else "未开启成人内容")
        checks.append(("登录态", "ok", f"{name}（@{pid}）　{'／'.join(extra)}"))
        if not info.get("adult"):
            res["advice"].append(
                "pixiv 账号设置里未开启「成人内容」→ R-18 作品仍会拿不到，"
                "到 pixiv 设置 → 浏览限制 里打开")
    else:
        checks.append(("登录态", "bad", "凭据无效或已过期 → 实际按匿名处理"))
        res["advice"].append("重新获取 PHPSESSID（浏览器登录 pixiv 后复制 cookie）")

    # ---- ③ 可读取量：登录 vs 匿名 ----
    say(f"② 实测可读取量（关键词「{keyword}」，匿名与登录各翻最多 {max_pages} 页）…")
    tick(f"正在实测可读取量：最多各翻 {max_pages} 页，请稍候…")

    def measure(cli: "PixivClient", stop_after: int, label: str = "") -> Tuple[int, Optional[int]]:
        """测量某关键词下能翻到多少唯一作品。

        stop_after：拿到这么多就不用再翻了 —— 匿名上限只有约 600，
        一旦超过它就足以证明"这个客户端不是匿名"，没必要继续翻到底。
        """
        seen: Set[str] = set()
        total: Optional[int] = None
        for page in range(1, max_pages + 1):
            if label:
                tick(f"正在实测{label}可读取量：第 {page}/{max_pages} 页"
                     f"（已找到 {len(seen)} 个作品）…")
            try:
                items, extra = cli.search_illusts(keyword, page, order="date", mode="all",
                                                  s_mode="s_tag")
            except CrawlError:
                break
            if total is None:
                total = as_int(extra.get("total"), 0) or None
            if not items:
                break
            before = len(seen)
            seen.update(str(x.get("id")) for x in items)
            if len(seen) == before:
                break            # 整页重复 -> 到此为止
            if len(seen) >= stop_after:
                break
            time.sleep(float(cfg.get("search_delay") or 0.3))
        return len(seen), total

    # 登录侧只要超过匿名上限一大截就够判定了；匿名侧翻到上限即可
    if info:
        got_login, total_login = measure(own, stop_after=ANON_CAP * 2 + 100, label="登录")
    else:
        got_login, total_login = 0, None
    if skip_anon:
        got_anon, total_anon = 0, None
    else:
        try:
            anon = PixivClient(HttpClient(cfg, verbose=False), cfg, verbose=False, anonymous=True)
            got_anon, total_anon = measure(anon, stop_after=ANON_CAP + 200, label="匿名")
        except Exception:  # noqa: BLE001
            got_anon, total_anon = 0, None

    ratio = (got_login / got_anon) if got_anon else 0.0
    capped = got_login >= max_pages * 50      # 触到本次探测的翻页上限
    # 两边都触到本次上限 -> 这次测不出差别（说明探测深度不够），不能因此判定"登录没生效"
    inconclusive = (not skip_anon) and capped and got_anon >= max_pages * 50 - 10
    res["capacity"] = {
        "login": got_login, "anon": got_anon, "ratio": round(ratio, 2),
        "total_login": total_login, "total_anon": total_anon,
        "capped": capped, "inconclusive": inconclusive, "max_pages": max_pages,
        "skipped_anon": bool(skip_anon),
    }
    say(f"   登录可读 {got_login}，匿名可读 {got_anon}，倍数 {ratio:.1f}"
        + ("（两边都到探测上限，结论不可靠，建议加大深度）" if inconclusive else "")
        + f"，pixiv 报告总数 登录={total_login} 匿名={total_anon}")

    # 判定顺序很重要：先把"确实超过匿名上限"这个硬证据挑出来（与探测深度无关），
    # 再判"测不出来"（深度不够），最后才判"登录没生效"。
    # 早期把"登录数<=匿名上限"排在"测不出来"前面，导致浅探测时明明两边同时触顶、
    # 却给出"登录没生效、重新导入 cookie"的误导建议（实测踩到）。
    if not info:
        checks.append(("可读取量", "bad",
                       f"仅匿名：本关键词约 {got_anon} 个（每种排序上限约 {ANON_CAP}）"))
    elif got_login > ANON_CAP * 1.5:
        # 超过匿名上限一大截 —— 这是登录权限生效最硬的证据（与匿名上限无关的绝对判据）
        checks.append(("可读取量", "ok",
                       f"登录可读 {got_login}+ 个（已明显超过匿名上限 {ANON_CAP}），"
                       f"匿名仅 {got_anon} 个"))
    elif skip_anon:
        checks.append(("可读取量", "warn",
                       f"登录可读 {got_login} 个（本次跳过了匿名对比，"
                       "没有对比就看不出提升倍数）"))
        res["advice"].append("取消「跳过匿名对比」再测一次，才能看出登录带来了多少提升")
    elif inconclusive:
        checks.append(("可读取量", "warn",
                       f"两边都翻到了探测上限（登录 {got_login}／匿名 {got_anon}），"
                       "这次没能测出差别"))
        # 这是"测得太浅"，不是登录有问题 —— 不要误导用户去重新导入 cookie。
        # 这里只用界面无关的措辞，具体怎么调深度由各界面自己补。
        res["advice"].append(
            f"加大探测深度到 {max(20, max_pages * 3)} 页以上再测一次"
            "（登录后可翻到 6000+ 个；探测太浅时登录与匿名会同时触顶）")
    elif got_login <= ANON_CAP + 50:
        checks.append(("可读取量", "bad",
                       f"登录后仅 {got_login} 个，几乎等于匿名上限 {ANON_CAP} → "
                       "登录可能没真正生效"))
        res["advice"].append("登录态显示有效但可读量没有提升，试着重新导入 cookie")
    else:
        checks.append(("可读取量", "ok",
                       f"登录可读 {got_login} 个，匿名 {got_anon} 个（{ratio:.1f} 倍）"))

    # ---- ④ 原图下载权 ----
    if check_download:
        say("③ 校验原图下载权…")
        tick("正在校验原图下载权限…")
        dl: Dict[str, Any] = {"ok": False, "note": ""}
        try:
            items, _extra = own.search_illusts(keyword, 1, order="date", mode="all",
                                               s_mode="s_tag")
            pick = next((x for x in items if as_int(x.get("illustType"), 0) != 2), None)
            if pick is None:
                dl["note"] = "没找到可用于测试的静态作品"
            else:
                iid = str(pick.get("id"))
                dl["id"] = iid
                urls = own.resolve_original_urls(iid, detail=None)
                first = next((u for u in urls if not u.get("is_ugoira")), None)
                if not first:
                    dl["note"] = "没能解析出原图地址"
                else:
                    dl["url_host"] = urllib.parse.urlparse(first["url"]).netloc
                    status, ctype, size = probe_url_size(own.http, cfg, first["url"])
                    dl["status"] = status
                    dl["ctype"] = ctype
                    if status == 200 and size > 0:
                        dl.update({"ok": True, "bytes": size})
                    else:
                        dl["note"] = f"原图地址无法下载（HTTP {status}）——可能需要登录或已被限制"
        except CrawlError as exc:
            dl["note"] = f"请求失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            dl["note"] = f"{type(exc).__name__}: {exc}"
        res["download"] = dl
        if dl.get("ok"):
            checks.append(("原图下载", "ok",
                           f"作品 {dl.get('id')} 原图可下载（{format_size(as_int(dl.get('bytes'), 0))}）"))
        else:
            checks.append(("原图下载", "bad", dl.get("note") or "失败"))
            res["advice"].append("原图取不到：确认 cookie 有效、账号已开启成人内容，或换代理试试")

    # ---- 结论 ----
    bad = [c for c in checks if c[1] == "bad"]
    warn = [c for c in checks if c[1] == "warn"]
    if not info:
        res["verdict"] = "anonymous"
    elif bad:
        res["verdict"] = "limited"
    elif warn:
        res["verdict"] = "limited"
    else:
        res["verdict"] = "full"
    if res["verdict"] == "full":
        res["advice"] = ["一切正常，可以全量爬取。"]
    return res


def pkce_pair() -> Tuple[str, str]:
    """生成 PKCE 的 code_verifier / code_challenge（S256），只用标准库。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    return verifier, challenge


def build_authorize_url(challenge: str) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "client": "pixiv-android",
    })


def extract_code_from_redirect(text: str) -> str:
    """从用户粘贴的内容里取出授权码：支持完整回调 URL、只含参数的串、或裸 code。"""
    s = str(text or "").strip().strip("「」").strip("'")
    if not s:
        return ""
    if "code=" in s:
        query = urllib.parse.urlparse(s).query or s.split("?", 1)[-1]
        params = urllib.parse.parse_qs(query)
        if params.get("code"):
            return str(params["code"][0]).strip()
    # 裸 code：授权码是 URL 安全的 base64-ish 串，长度通常在 20 以上
    if re.fullmatch(r"[A-Za-z0-9_\-\.]{16,}", s):
        return s
    return ""


# --------------------------------------------------------------------------------------
# 索引（jsonl 主库 + csv + md + sqlite）
# --------------------------------------------------------------------------------------


class Library:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.originals = self.root / "_originals"
        self.by_tag = self.root / "by_tag"
        self.by_author = self.root / "by_author"
        self.by_keyword = self.root / "by_keyword"
        self.index_dir = self.root / "_index"
        self.state_dir = self.root / "_state"
        self.records_path = self.index_dir / "records.jsonl"
        self.csv_path = self.index_dir / "index.csv"
        self.md_path = self.index_dir / "index.md"
        self.sqlite_path = self.index_dir / "catalog.sqlite"
        self.queries_path = self.index_dir / "queries.csv"
        self._lock = threading.Lock()

    def ensure(self) -> None:
        for p in (self.root, self.originals, self.index_dir):
            ensure_dir(p)

    # ---- 读写 ----

    def load_records(self) -> List[Dict[str, Any]]:
        recs: List[Dict[str, Any]] = []
        if not os.path.isfile(native_path(self.records_path)):
            return recs
        with open(native_path(self.records_path), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    recs.append(obj)
        merge_dtags(recs, self.index_dir)   # 衍生标签只在读取时合并
        return recs

    def known_ids(self) -> Dict[str, int]:
        """{illust_id: 已下载页数}"""
        res: Dict[str, int] = {}
        for r in self.load_records():
            iid = str(r.get("id") or "")
            if not iid:
                continue
            res[iid] = max(res.get(iid, 0), int(r.get("page", 0)) + 1)
        return res

    def known_ids_fast(self) -> Dict[str, int]:
        """只读 jsonl 前几列的精简解析，速度更快。"""
        res: Dict[str, int] = {}
        path = self.records_path
        if not os.path.isfile(native_path(path)):
            return res
        id_re = re.compile(r'"id"\s*:\s*"(\d+)"')
        page_re = re.compile(r'"page"\s*:\s*(\d+)')
        with open(native_path(path), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = id_re.search(line)
                if not m:
                    continue
                pm = page_re.search(line)
                page = int(pm.group(1)) if pm else 0
                iid = m.group(1)
                res[iid] = max(res.get(iid, 0), page + 1)
        return res

    def append_records(self, recs: Sequence[Dict[str, Any]]) -> None:
        if not recs:
            return
        ensure_dir(self.index_dir)
        with self._lock:
            with open(native_path(self.records_path), "a", encoding="utf-8", newline="\n") as fh:
                for r in recs:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def upsert_records(self, recs: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
        """按 (id,page) 去重合并写入。返回 (新增, 更新)。"""
        if not recs:
            return 0, 0
        key = lambda r: (str(r.get("id")), int(r.get("page") or 0))  # noqa: E731
        existing = {key(r): r for r in self.load_records()}
        added = updated = 0
        for r in recs:
            k = key(r)
            if k in existing:
                merged = dict(existing[k])
                merged.update({kk: vv for kk, vv in r.items() if vv not in (None, "", [], {})})
                existing[k] = merged
                updated += 1
            else:
                existing[k] = r
                added += 1
        ensure_dir(self.index_dir)
        tmp = self.records_path.with_name("records.jsonl.tmp")
        with open(native_path(tmp), "w", encoding="utf-8", newline="\n") as fh:
            for r in existing.values():
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(native_path(tmp), native_path(self.records_path))
        return added, updated

    def log_query(self, keyword: str, downloaded: int, skipped: int, failed: int, pages: int) -> None:
        ensure_dir(self.index_dir)
        new = not os.path.isfile(native_path(self.queries_path))
        with open(native_path(self.queries_path), "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["time", "keyword", "downloaded", "skipped", "failed", "pages"])
            w.writerow([now_iso(), keyword, downloaded, skipped, failed, pages])

    # ---- 导出 ----

    def export_csv(self, recs: Optional[Sequence[Dict[str, Any]]] = None) -> Path:
        recs = list(recs if recs is not None else self.load_records())
        fields = ["id", "page", "title", "author", "author_id", "type", "tags", "query",
                  "width", "height", "bytes", "ext", "file", "page_url", "url", "create_date",
                  "bookmark_count", "like_count", "view_count", "x_restrict", "crawled_at"]
        with open(native_path(self.csv_path), "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(fields)
            for r in recs:
                row = []
                for f in fields:
                    v = r.get(f)
                    if isinstance(v, list):
                        v = "|".join(str(x) for x in v)
                    row.append("" if v is None else v)
                w.writerow(row)
        return self.csv_path

    def build_sqlite(self, recs: Optional[Sequence[Dict[str, Any]]] = None) -> Path:
        recs = list(recs if recs is not None else self.load_records())
        if os.path.exists(native_path(self.sqlite_path)):
            os.remove(native_path(self.sqlite_path))
        columns = [
            "id", "page", "title", "author", "author_id", "type",
            "tags", "tags_flat", "query", "width", "height",
            "bytes", "ext", "file", "page_url", "url",
            "create_date", "bookmark_count", "like_count",
            "view_count", "x_restrict", "crawled_at",
        ]
        conn = sqlite3.connect(native_path(self.sqlite_path))
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute(f"CREATE TABLE works ({', '.join(columns)}, PRIMARY KEY (id, page))")
            rows = []
            for r in recs:
                tags = r.get("tags") or []
                if isinstance(tags, str):
                    tags = [tags]
                rows.append((
                    str(r.get("id")), int(r.get("page") or 0), r.get("title"), r.get("author"),
                    r.get("author_id"), r.get("type"), json.dumps(tags, ensure_ascii=False),
                    " ".join(str(t) for t in tags), r.get("query"), r.get("width"), r.get("height"),
                    r.get("bytes"), r.get("ext"), r.get("file"), r.get("page_url"), r.get("url"),
                    r.get("create_date"), r.get("bookmark_count"), r.get("like_count"),
                    r.get("view_count"), r.get("x_restrict"), r.get("crawled_at"),
                ))
            conn.executemany(
                f"INSERT OR REPLACE INTO works VALUES ({','.join(['?'] * len(columns))})", rows
            )
            for ddl in (
                "CREATE INDEX idx_author ON works(author)",
                "CREATE INDEX idx_query ON works(query)",
                "CREATE INDEX idx_tags ON works(tags_flat)",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.Error:
                    pass
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE works_fts USING fts5("
                    "id UNINDEXED, title, tags_flat, author, tokenize='unicode61')"
                )
                conn.executemany(
                    "INSERT INTO works_fts (id, title, tags_flat, author) VALUES (?,?,?,?)",
                    [(str(r.get("id")), str(r.get("title") or ""),
                      " ".join(str(t) for t in (r.get("tags") or [])), str(r.get("author") or ""))
                     for r in recs],
                )
            except sqlite3.Error:
                pass  # 该 Python 未编译 FTS5 时跳过，不影响主检索
            conn.commit()
        finally:
            conn.close()
        return self.sqlite_path

    def export_markdown(self, recs: Optional[Sequence[Dict[str, Any]]] = None,
                        top_tags: int = 60) -> Path:
        recs = list(recs if recs is not None else self.load_records())
        by_work: Dict[str, List[Dict[str, Any]]] = {}
        for r in recs:
            by_work.setdefault(str(r.get("id")), []).append(r)
        tag_count: Dict[str, int] = {}
        author_count: Dict[str, int] = {}
        kw_count: Dict[str, int] = {}
        for r in recs:
            for t in r.get("tags") or []:
                tag_count[str(t)] = tag_count.get(str(t), 0) + 1
            a = str(r.get("author") or "未知")
            author_count[a] = author_count.get(a, 0) + 1
            for k in r.get("query_keys") or ([r.get("query")] if r.get("query") else []):
                kw_count[str(k)] = kw_count.get(str(k), 0) + 1
        total_bytes = sum(int(r.get("bytes") or 0) for r in recs)
        top = sorted(tag_count.items(), key=lambda kv: (-kv[1], kv[0]))[:top_tags]
        lines = [
            "# pixiv 本地图库索引",
            "",
            f"- 生成时间：{now_iso()}",
            f"- 作品数：**{len(by_work)}**　图片文件数：**{len(recs)}**　占用：**{format_size(total_bytes)}**",
            f"- 画师数：{len(author_count)}　不同标签数：{len(tag_count)}",
            "",
            "## 按关键词",
            "",
            "| 关键词 | 图数 | 目录 |",
            "| --- | ---: | --- |",
        ]
        for k, c in sorted(kw_count.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| {k} | {c} | [by_keyword/{kw_key(k)}](../by_keyword/{urllib.parse.quote(kw_key(k))}) |")
        lines += ["", "## 热门标签（Top %d）" % top_tags, "",
                  "| 标签 | 图数 | 目录 |", "| --- | ---: | --- |"]
        for t, c in top:
            d = tag_dir_name(t)
            lines.append(f"| {t} | {c} | [by_tag/{d}](../by_tag/{urllib.parse.quote(d)}) |")
        lines += ["", "## 画师（按图数）", "", "| 画师 | 图数 | 目录 |", "| --- | ---: | --- |"]
        for a, c in sorted(author_count.items(), key=lambda kv: (-kv[1], kv[0]))[:top_tags]:
            aid = ""
            for r in recs:
                if str(r.get("author") or "未知") == a:
                    aid = str(r.get("author_id") or "")
                    break
            d = author_dir_name(a, aid)
            lines.append(f"| {a} | {c} | [by_author/{d}](../by_author/{urllib.parse.quote(d)}) |")
        lines += [
            "",
            "## 检索方式",
            "",
            "```powershell",
            r"python pixiv_crawler.py search 初音            # 关键词模糊检索",
            r"python pixiv_crawler.py search --tag 風景       # 按标签",
            r"python pixiv_crawler.py search --author 某某     # 按画师",
            r"python pixiv_crawler.py search --list-tags 50    # 列出高频标签",
            "```",
            "",
            ">" + " 每张图片都有一份 sidecar JSON（`_meta/<id>_p<n>.json`），含标题、画师、标签、原图直链、作品页地址。",
            "",
        ]
        ensure_dir(self.md_path.parent)
        with open(native_path(self.md_path), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
        return self.md_path

    def write_meta(self, rec: Dict[str, Any]) -> Path:
        p = self.root / "_meta" / f"{rec['id']}_p{rec.get('page', 0)}.json"
        atomic_write_json(p, rec)
        return p

    def rebuild_all(self) -> Dict[str, Any]:
        recs = self.load_records()
        self.export_csv(recs)
        self.export_markdown(recs)
        self.build_sqlite(recs)
        return {
            "records": len(recs),
            "csv": str(self.csv_path),
            "md": str(self.md_path),
            "sqlite": str(self.sqlite_path),
        }


# --------------------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------------------


# 内容分级：pixiv 用 xRestrict 表示，0=全年龄, 1=R-18, 2=R-18G（猎奇）
R18_LEVELS = (0, 1, 2)


def normalize_artist_id(text: Any) -> str:
    """把用户输入统一成画师 ID 字符串。

    接受：纯数字 ID、主页链接、member.php 链接、带空格的输入。
    返回 "" 表示看不懂（调用方据此提示用户）。
    """
    s = str(text or "").strip()
    if not s:
        return ""
    for pat in (r"/users/(\d+)", r"/member\.php\?id=(\d+)", r"/member_illust\.php\?id=(\d+)",
                r"[?&]id=(\d+)", r"^(\d{2,})$"):
        m = re.search(pat, s)
        if m:
            return m.group(1)
    return ""
R18_LEVEL_NAMES = {0: "全年龄", 1: "R-18", 2: "R-18G"}

# 库检索用的「分级模式」-> 允许哪些 xRestrict
#   6 种勾选组合的语义见 r18_levels_to_mode()
R18_MODE_LEVELS: Dict[str, Tuple[int, ...]] = {
    "all": (0, 1, 2),        # 全年龄 + R-18 + R-18G
    "no_g": (0, 1),          # 全年龄 + R-18（不要猎奇）
    "only_r18": (1,),        # 只要 R-18
    "only_g": (2,),          # 只要 R-18G
    "only_g_mix": (0, 2),    # 全年龄 + R-18G（跳过 R-18）
    "hide": (0,),            # 只要全年龄（默认）
    "r18_all": (1, 2),       # R-18 + R-18G
    "none": (),              # 什么都不选（不应爬取）
}


def r18_levels_to_mode(levels: Sequence[int]) -> str:
    """把用户勾选的级别集合（0/1/2 的任意子集）映射成分级模式名。

    用户可以在界面上自由勾选「全年龄 / R-18 / R-18G」三个标签，
    共 2³ = 6 种有意义的组合（全不勾是"什么都不爬"，会被上层拦下）：

        勾选            模式         含义
        ─────────────────────────────────────────────
        0,1,2           all          全都要（默认）
        0,1             no_g         全年龄 + R-18，不要猎奇
        1,2             r18_all      只要 R-18 与 R-18G
        0,2             only_g_mix   全年龄 + R-18G（跳过 R-18）
        0               hide         只要全年龄
        1               only_r18     只要 R-18
        2               only_g       只要 R-18G
        （空）           none         什么都不爬
    """
    s = tuple(sorted(set(int(x) for x in levels if int(x) in R18_LEVELS)))
    return {
        (0, 1, 2): "all",
        (0, 1): "no_g",
        (1, 2): "r18_all",
        (0, 2): "only_g_mix",
        (0,): "hide",
        (1,): "only_r18",
        (2,): "only_g",
        (): "none",
    }.get(s, "all")


def pass_r18_mode(xr: int, mode: str) -> bool:
    """这个 xRestrict 在给定分级模式下是否应该收。

    与 GUI/CLI 的分级模式一一对应；未知模式按"全都要"处理（宽松失败）。
    """
    allowed = R18_MODE_LEVELS.get(mode)
    if allowed is None:
        allowed = R18_MODE_LEVELS["all"]
    return int(xr) in allowed


def r18_plan(levels: Sequence[int]) -> Tuple[str, Tuple[int, ...]]:
    """用户勾选的分级 -> (传给 pixiv 的搜索 mode, 本站再筛的允许级别)。

    服务器 mode 只能三选一（all / safe / r18），粒度是"R18 全都要或全不要"，
    没法在服务器上区分 R-18 与 R-18G（两者都是 R18）。
    所以策略是：**选一个能覆盖用户所选、又尽量小的服务器模式**，剩下的在本地精筛。
    这样既能少下无用内容，又保证不漏。

        勾选              服务器 mode   本地允许
        ─────────────────────────────────────────
        只有全年龄          safe          {0}
        只有 R-18          r18           {1}
        只有 R-18G         r18           {2}
        全年龄 + R-18       safe+r18      {0,1}   → 服务器没有这种模式，用 all
        全年龄 + R-18G      all           {0,2}   → all 是唯一能同时拿到 0 和 2 的
        R-18 + R-18G       r18           {1,2}
        三个都要            all           {0,1,2}
    """
    s = tuple(sorted(set(int(x) for x in levels if int(x) in R18_LEVELS)))
    if not s:
        return "safe", ()
    if s == (0,):
        return "safe", (0,)
    if s == (1,):
        return "r18", (1,)
    if s == (2,):
        return "r18", (2,)
    if s == (1, 2):
        return "r18", (1, 2)
    # (0,1) 与 (0,2) 与 (0,1,2)：服务器都没有"只排除某一个"的模式，只能用 all
    return "all", s


def level_counts(records: Sequence[Dict[str, Any]]) -> Dict[int, int]:
    """统计各分级的作品数（按作品去重）。用于界面上显示"各分级有多少"。"""
    seen: Dict[str, int] = {}
    for r in records:
        iid = str(r.get("id") or "")
        if iid and iid not in seen:
            seen[iid] = as_int(r.get("x_restrict"), 0)
    counts = {0: 0, 1: 0, 2: 0}
    for lvl in seen.values():
        counts[lvl] = counts.get(lvl, 0) + 1
    return counts


def search_records_advanced(
    recs: Sequence[Dict[str, Any]],
    *,
    text: str = "",
    tags: Sequence[str] = (),
    tags_match_all: bool = True,
    author: str = "",
    author_ids: Sequence[str] = (),
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    min_likes: int = 0,
    min_bookmarks: int = 0,
    r18_mode: str = "hide",
    ai_mode: str = "all",
    d_tags: Sequence[str] = (),
    d_tags_match_all: bool = True,
    limit: int = 0,
    sort_by: str = "default",
) -> List[Dict[str, Any]]:
    """带完整筛选维度的检索（图形界面的库检索面板与 CLI 共用）。

    与 search_records 的区别：支持发布时间范围、点赞/收藏门槛、R-18 分级、
    标签"全含/任一"两种匹配方式、衍生标签筛选，以及排序。

    r18_mode: 与爬取页的三个勾选框一一对应（见 R18_MODE_LEVELS）：
              hide=只要全年龄（默认）／no_g=全年龄+R-18／only_r18=只要 R-18
              only_g=只要 R-18G／only_g_mix=全年龄+R-18G／r18_all=R-18+R-18G
              all=全都要／none=都不要
    ai_mode:  all=不限／exclude=排除 AI 生成／only=只要 AI 生成
    d_tags:   衍生标签筛选（用户自己加的那一层，含自动的「第N次爬取」）
    """
    text_l = (text or "").strip().lower()
    tags_l = [t.strip().lower() for t in tags if t.strip()]
    d_tags_l = [t.strip().lower() for t in d_tags if t.strip()]
    author_l = author.strip().lower()
    # 画师 ID：精确匹配（库里存的是 author_id），支持传主页链接或纯 ID
    want_aids = {normalize_artist_id(a) for a in author_ids if str(a).strip()}
    want_aids.discard("")
    query_l = query.strip().lower()
    rng = parse_range({"date_from": date_from, "date_to": date_to})
    ai_mode = (ai_mode or "all").lower()

    def pass_r18(xr: int, mode: str) -> bool:
        return pass_r18_mode(xr, mode)

    hits: List[Dict[str, Any]] = []
    for r in recs:
        xr = as_int(r.get("x_restrict"), 0)
        if not pass_r18(xr, r18_mode):
            continue
        if ai_mode != "all":
            is_ai = as_int(r.get("ai"), 0) != 0
            if (ai_mode == "only" and not is_ai) or (ai_mode == "exclude" and is_ai):
                continue
        r_tags = [str(t) for t in (r.get("tags") or [])]
        if tags_l:
            lows = {t.lower() for t in r_tags}
            if tags_match_all:
                if not all(x in lows for x in tags_l):
                    continue
            elif not any(x in lows for x in tags_l):
                continue
        r_dtags = [str(t) for t in (r.get("d_tags") or [])]
        if d_tags_l:
            dlow = {t.lower() for t in r_dtags}
            if d_tags_match_all:
                if not all(x in dlow for x in d_tags_l):
                    continue
            elif not any(x in dlow for x in d_tags_l):
                continue
        if author_l and author_l not in str(r.get("author") or "").lower():
            continue
        if query_l and query_l != str(r.get("query") or "").lower():
            continue
        if want_aids and str(r.get("author_id") or "").strip() not in want_aids:
            continue
        if rng.active and not rng.contains(parse_illust_datetime(r.get("create_date"))):
            continue
        if min_likes > 0 and as_int(r.get("like_count"), 0) < min_likes:
            continue
        if min_bookmarks > 0 and as_int(r.get("bookmark_count"), 0) < min_bookmarks:
            continue
        if text_l:
            hay = " ".join([
                str(r.get("title") or ""), str(r.get("author") or ""), " ".join(r_tags),
                " ".join(r_dtags),
                " ".join(str(k) for k in (r.get("query_keys") or [])), str(r.get("id") or ""),
                str(r.get("query") or ""),
                " ".join(str(v) for v in (r.get("tags_i18n") or {}).values()),
            ]).lower()
            if text_l not in hay:
                continue
        hits.append(r)

    if sort_by == "likes":
        hits.sort(key=lambda r: -as_int(r.get("like_count"), 0))
    elif sort_by == "bookmarks":
        hits.sort(key=lambda r: -as_int(r.get("bookmark_count"), 0))
    elif sort_by == "date":
        hits.sort(key=lambda r: str(r.get("create_date") or ""), reverse=True)
    elif sort_by == "size":
        hits.sort(key=lambda r: -as_int(r.get("bytes"), 0))
    else:
        hits.sort(key=lambda r: (str(r.get("author") or ""), str(r.get("id")),
                                 as_int(r.get("page"), 0)))
    return hits[:limit] if limit and limit > 0 else hits


def tag_histogram(recs: Sequence[Dict[str, Any]]) -> List[Tuple[str, int]]:
    """统计图库里的标签及出现次数，按次数从多到少（给界面的标签列表用）。"""
    counter: Dict[str, int] = {}
    for r in recs:
        for t in r.get("tags") or []:
            s = str(t)
            counter[s] = counter.get(s, 0) + 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def dtag_histogram(recs: Sequence[Dict[str, Any]]) -> List[Tuple[str, int]]:
    """统计衍生标签（含自动的「第N次爬取」）及出现次数，按次数从多到少。"""
    counter: Dict[str, int] = {}
    for r in recs:
        for t in r.get("d_tags") or []:
            s = str(t)
            counter[s] = counter.get(s, 0) + 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))


def author_histogram(recs: Sequence[Dict[str, Any]]) -> List[Tuple[str, str, int]]:
    """统计图库里的画师（ID, 名, 出现次数），按次数从多到少。"""
    counter: Dict[str, List[Any]] = {}
    for r in recs:
        aid = str(r.get("author_id") or "").strip()
        if not aid:
            continue
        name = str(r.get("author") or "").strip() or aid
        e = counter.setdefault(aid, [name, 0])
        if not e[0]:
            e[0] = name
        e[1] += 1
    return sorted(((aid, e[0], e[1]) for aid, e in counter.items()),
                  key=lambda kv: (-kv[2], kv[1]))


# --------------------------------------------------------------------------------------
# 衍生标签（dtags）与爬取历史
#
# 衍生标签 = 用户自己加在作品上的标签层，与原标签分开存放：
#     _index/dtags.json            {作品ID: [标签, ...] }   按作品存（一图多页共享）
#     _index/crawl_history.jsonl   每次爬取一条记录（序号/时间/检索项/结果）
# 原标签来自 pixiv、不可改；衍生标签可增删，还能一键导入原标签当起点。
# 「第N次爬取」就是自动附加的衍生标签，N 全局累计，跨关键词/画师/全量/追更共用。
# --------------------------------------------------------------------------------------

DTAGS_FILE = "dtags.json"
HISTORY_FILE = "crawl_history.jsonl"


def dtags_path(index_dir: Path) -> Path:
    return Path(index_dir) / DTAGS_FILE


def history_path(index_dir: Path) -> Path:
    return Path(index_dir) / HISTORY_FILE


def load_dtags(index_dir: Path) -> Dict[str, List[str]]:
    """读取衍生标签：{作品ID: [标签...]}。文件不存在返回空。"""
    data = read_json(dtags_path(index_dir), {}) or {}
    return {str(k): [str(t) for t in v] for k, v in data.items() if isinstance(v, list)}


def work_dtags(index_dir: Path, work_id: Any) -> List[str]:
    return list(load_dtags(index_dir).get(str(work_id), []))


def set_work_dtags(index_dir: Path, work_id: Any, tags: Sequence[str]) -> None:
    """覆盖某作品的衍生标签（保留顺序，去重）。"""
    data = load_dtags(index_dir)
    cleaned = []
    for t in tags:
        s = str(t).strip()
        if s and s not in cleaned:
            cleaned.append(s)
    data[str(work_id)] = cleaned
    atomic_write_json(dtags_path(index_dir), data)


def add_work_dtags(index_dir: Path, work_id: Any, tags: Sequence[str]) -> None:
    """往某作品追加若干衍生标签（已有则跳过）。"""
    cur = work_dtags(index_dir, work_id)
    for t in tags:
        s = str(t).strip()
        if s and s not in cur:
            cur.append(s)
    set_work_dtags(index_dir, work_id, cur)


def merge_dtags(recs: Sequence[Dict[str, Any]], index_dir: Path) -> None:
    """把衍生标签按作品 ID 合并进记录（就地改 dict，不写回 records.jsonl）。

    records.jsonl 里不存衍生标签 —— 它是「原数据」，应保持干净、可重放；
    衍生标签只在读取时合并，reindex 重建索引也不会丢。
    """
    tags = load_dtags(index_dir)
    for r in recs:
        r["d_tags"] = list(tags.get(str(r.get("id") or ""), []))


def load_crawl_history(index_dir: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    p = history_path(index_dir)
    if not os.path.isfile(native_path(p)):
        return entries
    with open(native_path(p), "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
    return entries


def append_crawl_history(index_dir: Path, entry: Dict[str, Any]) -> None:
    p = history_path(index_dir)
    ensure_dir(index_dir)
    with open(native_path(p), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def next_crawl_ordinal(index_dir: Path) -> int:
    """全局累计的「第N次爬取」序号：已有历史的最大序号 + 1。"""
    max_ord = 0
    for e in load_crawl_history(index_dir):
        o = as_int(e.get("ordinal"), 0)
        if o > max_ord:
            max_ord = o
    return max_ord + 1


def make_auto_tag(ordinal: int, when: Any = None) -> str:
    """「2026年9月30日第12次爬取」—— 自动附加到本次新下载作品的衍生标签。"""
    dt = parse_user_date(when) or datetime.now()
    return f"{dt.year}年{dt.month}月{dt.day}日第{ordinal}次爬取"


def finalize_run_record(session: "CrawlSession", lib: "Library", *,
                        mode: str, keywords: Sequence[str] = (),
                        artists: Sequence[str] = (),
                        params: Optional[Dict[str, Any]] = None,
                        result: Optional[Dict[str, Any]] = None,
                        started: float = 0.0,
                        log=out) -> Optional[str]:
    """爬取收尾：写历史 + 给本次新下载作品附自动标签。

    返回自动标签文本（如「2026年9月30日第12次爬取」）；干跑或没有新作品时返回 None。
    由 run_crawl / run_full / 追更 sync 在结束时调用，作为统一的「收尾仪式」。
    """
    if getattr(session, "dry_run", False) or not session.new_works:
        return None
    try:
        ordinal = next_crawl_ordinal(lib.index_dir)
        auto_tag = make_auto_tag(ordinal)
        for wid in sorted(session.new_works):
            add_work_dtags(lib.index_dir, wid, [auto_tag])
        entry = {
            "ordinal": ordinal,
            "mode": mode or "keyword",
            "keywords": [str(k) for k in (keywords or [])],
            "artists": [str(a) for a in (artists or [])],
            "params": {str(k): str(v) for k, v in (params or {}).items()},
            "result": {str(k): (int(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
                                else v) for k, v in (result or {}).items()},
            "started_at": (datetime.fromtimestamp(started).astimezone().isoformat(
                timespec="seconds") if started else now_iso()),
            "ended_at": now_iso(),
            "duration_s": round(time.time() - started, 1) if started else 0,
            "new_works": sorted(session.new_works),
            "auto_tag": auto_tag,
        }
        append_crawl_history(lib.index_dir, entry)
        log(f"\n[i] 已记录本次爬取：第 {ordinal} 次（{auto_tag}），"
            f"给 {len(session.new_works)} 个新作品附加了该衍生标签")
        return auto_tag
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] 写爬取历史失败（不影响已下载内容）：{type(exc).__name__}: {exc}")
        return None


def search_records(
    lib: Library,
    recs: Sequence[Dict[str, Any]],
    *,
    text: str = "",
    tags: Sequence[str] = (),
    author: str = "",
    query: str = "",
    limit: int = 50,
    ids: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    text = (text or "").strip().lower()
    tags_l = [t.strip().lower() for t in tags if t.strip()]
    author_l = author.strip().lower()
    query_l = query.strip().lower()
    ids_s = {str(i) for i in ids}
    hits = []
    for r in recs:
        if ids_s and str(r.get("id")) not in ids_s:
            continue
        r_tags = [str(t) for t in (r.get("tags") or [])]
        if tags_l and not all(any(t.lower() == x for t in r_tags) for x in tags_l):
            continue
        if author_l and author_l not in str(r.get("author") or "").lower():
            continue
        if query_l and query_l != str(r.get("query") or "").lower():
            continue
        if text:
            hay = " ".join([
                str(r.get("title") or ""), str(r.get("author") or ""),
                " ".join(r_tags), " ".join(str(k) for k in (r.get("query_keys") or [])),
                str(r.get("id") or ""), str(r.get("query") or ""),
                " ".join(str(v) for v in (r.get("tags_i18n") or {}).values()),
            ]).lower()
            if text not in hay:
                continue
        hits.append(r)
    hits.sort(key=lambda r: (str(r.get("author") or ""), str(r.get("id")), int(r.get("page") or 0)))
    return hits[:limit] if limit and limit > 0 else hits


def print_records(hits: Sequence[Dict[str, Any]], *, show_path: bool = False) -> None:
    if not hits:
        out("（无匹配结果）")
        return
    for i, r in enumerate(hits, 1):
        tags = "、".join(str(t) for t in (r.get("tags") or [])[:8])
        out(f"{i:>3}. {r.get('title')}")
        out(f"     画师: {r.get('author')} (id={r.get('author_id')})  作品: {r.get('id')} p{r.get('page')}"
            f"  {r.get('width')}x{r.get('height')}  {format_size(int(r.get('bytes') or 0))}")
        if tags:
            out(f"     标签: {tags}")
        out(f"     文件: {r.get('file')}")
        out(f"     页面: {r.get('page_url')}")
        if show_path:
            out(f"     完整: {os.path.abspath(str(r.get('file')))}")
        out("")


# --------------------------------------------------------------------------------------
# 爬取主流程
# --------------------------------------------------------------------------------------


class CrawlSession:
    def __init__(self, cfg: Dict[str, Any], lib: Library, *, verbose: bool = True) -> None:
        self.cfg = cfg
        self.lib = lib
        self.verbose = verbose
        self.http = HttpClient(cfg, verbose=verbose)
        self.client = PixivClient(self.http, cfg, verbose=verbose)
        self.new_records: List[Dict[str, Any]] = []
        self.new_works: Set[str] = set()      # 本次实际下载成功的作品 ID（用于附自动衍生标签）
        self.dry_run: bool = False
        self.rec_lock = threading.Lock()
        # ---- 可被 GUI 控制的停止/进度 ----
        # stop_event：GUI「停止」按钮置位；各收集循环在安全点检查它，
        # 一旦置位就优雅退出（已下载的图和索引都会保存，下次续跑）。
        self.stop_event = threading.Event()
        # progress_cb(dict)：GUI 通过它拿到当前进度（哪一步/完成了多少/总多少）。
        self.progress_cb: Optional[Any] = None
        self.known: Dict[str, int] = {}
        self.tag_counts: Dict[str, int] = {}
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.skipped_r18 = 0
        self.skipped_r18g = 0
        self.skipped_ugoira = 0
        self.force = False
        self.deep_all = False
        self.state: Optional[SessionState] = None
        # 全量分段爬取用的状态（段级断点续爬 + 收集缓冲）
        self.seg_state: SegmentStore = SegmentStore(lib.index_dir / "segments.json")
        self._seg_seen_ids: Set[str] = set()
        self._seg_collected: List[Dict[str, Any]] = []
        self.link_stats: Dict[str, int] = {}
        # 用户可调的筛选条件
        self.range: DateRange = parse_range(cfg)
        self.min_bookmarks: int = as_int(cfg.get("min_bookmarks"), 0)
        self.min_likes: int = as_int(cfg.get("min_likes"), 0)
        self.ai_only: bool = False       # 只要 AI 生成的作品
        self.ai_exclude: bool = False    # 排除 AI 生成的作品
        self.r18_levels: Tuple[int, ...] = R18_LEVELS   # 允许的 xRestrict 集合
        # 只收这些画师的作品（空=不限）。按 ID 精确匹配：
        # 搜索结果列表自带 userId，所以在列表阶段筛，被排除的连详情请求都省了。
        self.artist_ids: Set[str] = set()
        self.filter_stats: Dict[str, int] = {
            "like": 0, "date": 0, "date_stop_combo": 0, "unknown": 0,
            "skipped_r18": 0, "skipped_r18g": 0, "skipped_ugoira": 0, "skipped_ai": 0,
            "skipped_artist": 0,
        }
        self.stopwords = [str(s) for s in (cfg.get("tag_stopwords") or [])]

    # ---- 停止 / 进度（GUI 可控制）----

    def should_stop(self) -> bool:
        """在安全点检查：用户是否请求停止。"""
        return self.stop_event.is_set()

    def report(self, **kw: Any) -> None:
        """上报进度。kw 如 dict(stage='artist', done=1, total=5, detail='らいおん 45/628')。"""
        if self.progress_cb:
            try:
                self.progress_cb(kw)
            except Exception:  # noqa: BLE001
                pass

    # ---- 单个作品 ----

    def prepare_work(self, brief: Dict[str, Any], keyword: str, rank: int, order: str,
                     query_keys: Sequence[str]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        """抓详情，返回 (detail, 待下载任务列表)。任务项含 url/ext/dest/meta。"""
        iid = str(brief.get("id") or "")
        if not iid:
            return None, []
        # 时间范围：列表阶段就能判断，先筛掉可以省下一次详情请求
        created = parse_illust_datetime(brief.get("createDate"))
        if not self.range.contains(created):
            self.filter_stats["date"] += 1
            if self.verbose:
                shown = created.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d") if created else "未知"
                out(f"  - 跳过 {iid}（发布于 {shown}，不在 {self.range.describe()} 内）")
            return None, []
        # 画师 ID：搜索列表自带 userId，列表阶段就能筛，省掉详情请求
        if self.artist_ids:
            uid = str(brief.get("userId") or "").strip()
            if uid not in self.artist_ids:
                self.filter_stats["skipped_artist"] = self.filter_stats.get("skipped_artist", 0) + 1
                if self.verbose:
                    out(f"  - 跳过 {iid}（画师 {uid or '未知'} 不在指定范围内）")
                return None, []
        # AIGC（AI 生成）：搜索列表的 aiType 字段（1/2 = AI 生成，0 = 非 AI）。
        # 放在列表阶段筛，被排除的作品连详情请求都不用发 —— 全量爬取时这是几千次请求的差别。
        if self.ai_only or self.ai_exclude:
            is_ai = as_int(brief.get("aiType"), 0) != 0
            if (self.ai_only and not is_ai) or (self.ai_exclude and is_ai):
                self.filter_stats["skipped_ai"] = self.filter_stats.get("skipped_ai", 0) + 1
                if self.verbose:
                    out(f"  - 跳过 {iid}（{'AI 生成' if is_ai else '非 AI 生成'}，"
                        f"当前{'只收 AIGC' if self.ai_only else '排除 AIGC'}）")
                return None, []
        # pixiv 用 xRestrict 表示分级：0=全年龄, 1=R-18, 2=R-18G（猎奇）。
        # 实测确认：搜 R-18G 标签时 xRestrict 基本全是 2；正文里带 R-18G 标签的作品也是 2。
        # 用户的勾选（6 种组合）统一由 r18_levels 表达：不在集合里的一律不收。
        xr = int(brief.get("xRestrict") or 0)
        if xr not in self.r18_levels:
            if xr >= 2:
                self.skipped_r18g += 1
            elif xr >= 1:
                self.skipped_r18 += 1
            if self.verbose:
                out(f"  - 跳过 {R18_LEVEL_NAMES.get(xr, xr)} 作品 {iid}"
                    f"（当前只收 {'、'.join(R18_LEVEL_NAMES[x] for x in self.r18_levels)}）")
            return None, []
        # 兼容旧的 skip_r18 / skip_r18g 配置（CLI 未走 r18_levels 时仍然生效）
        if parse_bool(self.cfg.get("skip_r18"), True) and xr > 0:
            if xr >= 2:
                self.skipped_r18g += 1
            else:
                self.skipped_r18 += 1
            if self.verbose:
                out(f"  - 跳过 {'R-18G' if xr >= 2 else 'R-18'} 作品 {iid}（skip_r18=true）")
            return None, []
        if parse_bool(self.cfg.get("skip_r18g"), True) and xr >= 2:
            self.skipped_r18g += 1
            if self.verbose:
                out(f"  - 跳过 R-18G 作品 {iid}（skip_r18g=true；要收请加 --keep-r18g）")
            return None, []
        itype = brief.get("illustType")
        is_ugoira = (itype == 2) or (isinstance(itype, str) and itype.lower() == "ugoira")
        if is_ugoira and parse_bool(self.cfg.get("skip_ugoira"), True):
            self.skipped_ugoira += 1
            if self.verbose:
                out(f"  - 跳过动图(ugoira) {iid}（skip_ugoira=true）")
            return None, []

        detail = self.client.illust_detail(iid)
        if not detail:
            brief = dict(brief)
            brief["_missing"] = True
            if self.verbose:
                out(f"  - 作品 {iid} 详情 404（可能已删除/限制），跳过")
            return None, []

        # 人气门槛：搜索列表不带点赞/收藏数，只能拿到详情后判断（会多花一次请求）
        # 「收藏数」= pixiv 的 ブックマーク / bookmarkCount（作品页书签按钮旁的数字）
        # 「点赞数」= いいね / likeCount（爱心旁的数字）。两者互不相同，老作品常是点赞更多。
        if self.min_bookmarks > 0 or self.min_likes > 0:
            bk = as_int(detail.get("bookmarkCount"), -1)
            lk = as_int(detail.get("likeCount"), -1)
            if self.min_bookmarks > 0:
                if bk < 0:
                    self.filter_stats["unknown"] += 1
                    if self.verbose:
                        out(f"  - [warn] {iid} 未返回收藏数，无法按门槛判断，已放行")
                elif bk < self.min_bookmarks:
                    self.filter_stats["like"] += 1
                    if self.verbose:
                        out(f"  - 跳过 {iid}（收藏数 {bk} < {self.min_bookmarks}）")
                    return None, []
            if self.min_likes > 0:
                if lk < 0:
                    self.filter_stats["unknown"] += 1
                    if self.verbose:
                        out(f"  - [warn] {iid} 未返回点赞数，无法按门槛判断，已放行")
                elif lk < self.min_likes:
                    self.filter_stats["like"] += 1
                    if self.verbose:
                        out(f"  - 跳过 {iid}（点赞数 {lk} < {self.min_likes}）")
                    return None, []

        # AIGC 筛选：detail 的 aiType（1/2 = AI 生成，0 = 非 AI）。
        # 好消息：搜索结果列表本身就带 aiType，所以放在"列表阶段"筛更省请求
        # （见 filter_brief_by_ai），这里是详情阶段的双保险。
        if self.ai_only or self.ai_exclude:
            is_ai = as_int(detail.get("aiType"), 0) != 0
            if self.ai_only and not is_ai:
                self.filter_stats["skipped_ai"] = self.filter_stats.get("skipped_ai", 0) + 1
                if self.verbose:
                    out(f"  - 跳过 {iid}（非 AI 生成，当前只收 AIGC）")
                return None, []
            if self.ai_exclude and is_ai:
                self.filter_stats["skipped_ai"] = self.filter_stats.get("skipped_ai", 0) + 1
                if self.verbose:
                    out(f"  - 跳过 {iid}（AI 生成，当前排除 AIGC）")
                return None, []

        tags = tags_from_detail(detail, self.stopwords, int(self.cfg.get("max_tags_per_work") or 8))
        for t in tags:
            self.tag_counts[t["name"]] = self.tag_counts.get(t["name"], 0) + 1

        urls = self.client.resolve_original_urls(iid, detail)
        max_pages = int(self.cfg.get("max_pages_per_work") or 0)
        if max_pages > 0:
            urls = urls[:max_pages]
        if not urls:
            if self.verbose:
                out(f"  - 作品 {iid} 未取得原图地址，跳过")
            return None, []

        author_dir = author_dir_name(str(detail.get("userName") or ""), str(detail.get("userId") or ""))
        base_name = f"{iid}"
        # 动图：zip 已存在但用户要的动图格式还没生成时，仍然需要"处理"（触发转码）
        want_fmt = str(self.cfg.get("ugoira_format") or "none").lower().strip()
        tasks = []
        already = self.known.get(iid, 0)
        for idx, u in enumerate(urls):
            ext = u.get("ext") or guess_ext(u["url"])
            dest = self.lib.originals / author_dir / f"{base_name}_p{idx}{ext}"
            need = (not os.path.exists(native_path(dest))) or idx >= already
            if u.get("is_ugoira") and not need and want_fmt not in ("", "none", "zip"):
                anim = dest.with_suffix("." + want_fmt)
                if not os.path.exists(native_path(anim)):
                    need = True      # zip 在但动图没生成 -> 补转码
            # 缺页判断以「磁盘上到底有没有这个文件」为准：
            # 只看索引里的页数会把「上次用 --max-pages-per-work 只下了一页」误判成已下齐
            tasks.append({
                "illust_id": iid, "page_index": idx, "url": u["url"], "ext": ext,
                "dest": dest, "detail": detail, "tags": tags, "keyword": keyword,
                "rank": rank, "order": order, "query_keys": list(query_keys),
                "width": u.get("width") or detail.get("width"),
                "height": u.get("height") or detail.get("height"),
                "is_ugoira": bool(u.get("is_ugoira")),
                "frames": u.get("frames") or [],
                "need": need,
            })
        return detail, tasks

    def download_task(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        dest: Path = task["dest"]
        if not task["need"]:
            with self.rec_lock:
                self.skipped += 1
            return None
        if self.force and os.path.exists(native_path(dest)):
            # --force：删掉旧文件真正重新下载
            try:
                os.remove(native_path(dest))
            except OSError:
                pass
        ok, note = self.http.download(task["url"], dest)
        if not ok:
            with self.rec_lock:
                self.failed += 1
            out(f"    [fail] {task['illust_id']}_p{task['page_index']} {note} ({task['url']})")
            return None
        size = file_size(dest)
        try:
            rel = os.path.relpath(native_path(dest), native_path(self.lib.root))
        except ValueError:
            rel = str(dest)
        rec = build_record(
            illust_id=task["illust_id"], page_index=task["page_index"], detail=task["detail"],
            url=task["url"], ext=task["ext"], path_rel=rel, bytes_size=size,
            width=task["width"], height=task["height"], keyword=task["keyword"],
            tags=task["tags"], query_keys=task["query_keys"], rank=task["rank"], order=task["order"],
        )
        # ugoira：下载到的是 zip，接着按需转码、抽封面帧
        if task.get("is_ugoira"):
            self._finish_ugoira(rec, dest, task.get("frames") or [], task)
        with self.rec_lock:
            self.downloaded += 1
            self.new_records.append(rec)
        self.new_works.add(str(task["illust_id"]))
        extra = f"  → {rec['animated']}" if rec.get("animated") else ""
        out(f"    [ok] {rec['id']}_p{rec['page']} {format_size(size)}  "
            f"{rec['title'][:36]}{extra}")
        return rec

    def _finish_ugoira(self, rec: Dict[str, Any], zip_path: Path,
                       frames: Sequence[Dict[str, Any]], task: Dict[str, Any]) -> None:
        """动图收尾：转码成动图 + 抽出第一帧当封面，并把信息写进记录。"""
        rec["is_ugoira"] = True
        rec["frames"] = len(frames)
        rec["duration_ms"] = ugoira_mod.total_ms(frames) if (ugoira_mod and frames) else 0
        rec["animated"] = ""

        # 1) 抽第一帧作为静态封面 —— 分类目录里链接它，这样 by_tag 里能直接看图
        poster_rel = ""
        if ugoira_mod and frames:
            poster = zip_path.with_name(f"{rec['id']}_p{rec['page']}_poster.jpg")
            got = ugoira_mod.extract_poster(zip_path, poster, frames)
            if got:
                poster_rel = os.path.relpath(native_path(got), native_path(self.lib.root))
                rec["poster"] = poster_rel.replace("\\", "/")
                # 分类目录链接封面而不是 zip（zip 在图片浏览器里看不到内容）
                rec["file"] = rec["poster"]
                rec["ext"] = os.path.splitext(poster_rel)[1] or ".jpg"

        # 2) 转码
        fmt = str(self.cfg.get("ugoira_format") or "none").lower().strip()
        if not ugoira_mod:
            out("    [warn] 未找到 ugoira.py，跳过转码")
            return
        if fmt in ("", "none", "zip"):
            if frames:
                out(f"    [i] 动图已存为 zip（{len(frames)} 帧，"
                    f"{ugoira_mod.total_ms(frames)/1000:.2f} 秒）；"
                    "要自动转成动图请设 ugoira_format=webp 或 gif")
            return
        try:
            out_path, note = ugoira_mod.convert_ugoira(
                zip_path, frames, fmt,
                ffmpeg=str(self.cfg.get("ffmpeg_path") or "") or None,
                log=lambda m: out("   " + m))
        except Exception as exc:  # noqa: BLE001
            out(f"    [warn] 动图转码失败（已保留 zip）：{exc}")
            return
        if not out_path:
            out(f"    [warn] {note.splitlines()[0] if note else '转码未完成'}")
            out("          " + "\n          ".join(note.splitlines()[1:]))
            return
        rec["animated"] = f"{fmt} {format_size(file_size(out_path))}"
        rec["animated_path"] = os.path.relpath(native_path(out_path), native_path(self.lib.root))
        rec["animated_file"] = rec["animated_path"].replace("\\", "/")
        rec["file"] = rec["animated_file"]      # 分类目录里链接动图本体
        rec["ext"] = os.path.splitext(rec["animated_file"])[1] or f".{fmt}"
        out(f"    [ugoira] 已转码 {rec['id']} -> {Path(rec['animated_path']).name} ({note})")
        # 3) 是否保留原始 zip
        if not parse_bool(self.cfg.get("ugoira_keep_zip"), True):
            try:
                os.remove(native_path(zip_path))
                rec["zip_removed"] = True
                rec["file"] = rec["animated_file"]
            except OSError:
                pass

    # ---- 关键词 ----

    def _combos(self, order: str, s_mode: str, deep: bool) -> List[Tuple[str, str, str]]:
        """返回 [(说明文字, order, s_mode)]。deep=True 时组合多种排序与匹配方式。

        实测数据（关键词「初音ミク」，2026-09）：

          * **匿名**：单一排序最多 598 个，第 11 页起全部重复；加上「最早」可到约 1200。
          * **登录**：「最新(date_d)」翻 105 页仍有新内容，拿到 6300 个；
            「最早(date)」第 78 页起返回空，拿到 4620 个；
            **两者交集 0 个**，合并 10920 个 —— 比只用「最新」多 73%。
            所以登录后 deep **更**有意义（能翻得更深，两个区段各自都很长且不重叠）。
          * popular：匿名下与 date_d 完全重合（新增 0）；登录下**直接返回空**（该排序不可用）。
            所以默认不作为组合，只在 --deep-all 时才尝试，免得白发请求。
          * tag_full / popular_male / popular_female 实测新增 0，同样只在 --deep-all 时尝试。
        """
        if not deep:
            return [("", order, s_mode)]
        cands: List[Tuple[str, str, str]] = [
            (f"{ORDER_MAP.get(order, order)} + {S_MODE_MAP.get(s_mode, s_mode)}", order, s_mode),
            ("最早发布（与最新不重叠的区段）", "old", s_mode),
            ("热门（与最新重合时自动跳过）", "popular", "tag"),
        ]
        if self.deep_all:
            cands += [
                ("最新 + 标签完全一致", "date", "tag_full"),
                ("热门男性向", "popular_male", "tag"),
                ("热门女性向", "popular_female", "tag"),
            ]
        seen = set()
        res: List[Tuple[str, str, str]] = []
        for label, o, sm in cands:
            key = (o, sm)
            if key in seen:
                continue
            seen.add(key)
            res.append((label, o, sm))
        return res

    def _has_list_filter(self) -> bool:
        """是否存在"列表阶段就能判断"的筛选（决定要不要做跳跃探测）。"""
        return bool(self.range.active or self.ai_only or self.ai_exclude or self.artist_ids
                    or tuple(self.r18_levels) != R18_LEVELS)

    def _brief_passes(self, it: Dict[str, Any]) -> bool:
        """列表阶段筛选：这个简表能不能通过（与 prepare_work 的判断保持一致）。"""
        if self.range.active and not self.range.contains(
                parse_illust_datetime(it.get("createDate"))):
            return False
        if self.artist_ids and str(it.get("userId") or "").strip() not in self.artist_ids:
            return False
        if self.ai_only or self.ai_exclude:
            is_ai = as_int(it.get("aiType"), 0) != 0
            if (self.ai_only and not is_ai) or (self.ai_exclude and is_ai):
                return False
        if as_int(it.get("xRestrict"), 0) not in self.r18_levels:
            return False
        return True

    def probe_first_useful_page(self, keyword: str, order: str, s_mode: str, *,
                                probe_until: int = 0, step: int = 25,
                                stop_after_empty: int = 3) -> int:
        """「」跳跃探测：找出第一个「有内容能通过筛选"的页码。

        为什么需要：实测 pixiv 对「初音ミク」的**前 8 页(480 条)全是 AI 作品**。
        若用户选"排除 AI"，从第 1 页顺序翻要用 8 次请求才拿到第一个可用结果。
        这里按固定步长跳着采样，几次请求就能定位到有可用内容的深度。

        返回建议的起始页（1 表示从头翻就行）；-1 表示整个探测范围内都没有可用内容。
        """
        probe_until = probe_until or int(self.cfg.get("probe_pages") or 300)
        probe_until = max(step, min(probe_until, 1000))
        page = step
        empty = 0
        while page <= probe_until:
            try:
                items, _extra = self.client.search_illusts(
                    keyword, page, order=order, mode=self.cfg.get("mode") or "all", s_mode=s_mode)
            except (RateLimited, CrawlError):
                return 1
            if not items:
                empty += 1
                if empty >= stop_after_empty:
                    return -1
                page += step
                continue
            if any(self._brief_passes(it) for it in items if isinstance(it, dict)):
                return page
            page += step
        return -1

    def crawl_one_combo(
        self, keyword: str, order: str, s_mode: str, pages: int, seen_ids: Set[str],
        collected: List[Dict[str, Any]], *, label: str = "",
        stats: Optional[Dict[str, int]] = None, start_page: int = 1,
    ) -> Tuple[bool, Optional[int], List[str]]:
        """搜索一种排序/匹配组合并累积结果。返回 (是否提前终止, pixiv 报告总数, 相关标签)。

        stats：可选，回填 {"returned": 接口共返回多少条}
        方便调用方算出"这个组合的新增率"，从而发现某个组合其实白翻了。
        start_page：从第几页开始顺序翻（跳跃探测定位出的"有可用内容"的深度）。
        """
        related: List[str] = []
        total: Optional[int] = None
        filtered_streak = 0
        if label:
            out(f"  · 组合：{label}")
        for page in range(max(1, start_page), max(1, pages) + 1):
            if self.should_stop():
                out(f"    [停止] 用户请求停止，{keyword}／{label or order} 翻到第 {page} 页截断")
                break
            try:
                items, extra = self.client.search_illusts(keyword, page, order=order,
                                                          mode=self.cfg.get("mode") or "all", s_mode=s_mode)
            except CrawlError as exc:
                out(f"    [warn] 第 {page} 页搜索失败：{exc}")
                break
            if extra.get("total") and total is None:
                total = int(extra["total"])
            if extra.get("related_tags"):
                related = list(extra["related_tags"])[:20]
            if not items:
                out(f"    第 {page} 页没有更多结果，该组合结束")
                break
            if stats is not None:
                stats["returned"] = stats.get("returned", 0) + len(items)
            pre_seen = set(seen_ids)      # 本页处理前的快照，用来区分"重复"与"被筛掉"
            new = 0
            older_count = 0
            for it in items:
                iid = str(it.get("id") or "")
                if not iid or iid in seen_ids:
                    continue
                seen_ids.add(iid)
                it["_order"] = order
                it["_s_mode"] = s_mode
                it["_rank"] = len(seen_ids)
                collected.append(it)
                new += 1
                if self.range.active and self.range.is_older_than_range(
                        parse_illust_datetime(it.get("createDate"))):
                    older_count += 1
            out(f"    第 {page} 页：返回 {len(items)} 条，新增 {new} 条（累计 {len(collected)}）")
            # 从新到旧排序时，整页都比起始时间还早 -> 后面只会更早，不必再翻
            if self.range.active and items and older_count == len(items):
                out(f"    本页全部早于起始时间 {self.range.describe().split('~')[0].strip()}，"
                    "按从新到旧排序，后续只会更早 —— 停止翻页。")
                self.filter_stats["date_stop_combo"] += 1
                return False, total, related
            # "新增 0"有两种完全不同的原因，必须区分：
            #   ① 整页都是已见过的 ID -> 真的翻到接口尽头
            #   ② 整页都是"被筛掉的新作品"（AIGC/R-18/时间等）-> 后面很可能还有，不能停
            # 实测踩到：开启"排除 AI"后，pixiv 对「初音ミク」的前 8 页(480 条)全是 AI 作品，
            # 早期版本把这种局部空页当成"到尽头"，于是直接停下、用户以为没内容。
            if new == 0:
                if all(str(it.get("id") or "") in pre_seen for it in items):
                    out("    ⚠ 该组合已翻到接口能给的尽头（继续翻只会重复），本组合结束。")
                    out("      想拿更多作品：换 --order / --s-mode，或用 --deep 自动组合多种排序。")
                    return True, total, related
                filtered_streak += 1
                if filtered_streak <= 2 or filtered_streak % 10 == 0:
                    out(f"    · 本页 {len(items)} 条都是被筛选条件排除的新作品"
                        f"（连续 {filtered_streak} 页）；后面可能还有，继续翻。")
                if filtered_streak >= FILTERED_PAGE_PATIENCE:
                    out(f"    ⚠ 连续 {FILTERED_PAGE_PATIENCE} 页都是被排除的作品，"
                        "先停下（该条件在这个关键词下命中率极低）。"
                        "想继续可加大 --pages，或放宽 AIGC / 分级筛选。")
                    return False, total, related
            else:
                filtered_streak = 0
            if page < pages:
                time.sleep(float(self.cfg.get("search_delay") or 0.3))
        return False, total, related

    def _passing_count(self, collected: Sequence[Dict[str, Any]]) -> int:
        """在当前筛选条件下，有多少个作品真正符合（用于判断是否已达数量上限）。"""
        n = 0
        for it in collected:
            if self.range.active and not self.range.contains(
                    parse_illust_datetime(it.get("createDate"))):
                continue
            if self.ai_only and as_int(it.get("aiType"), 0) == 0:
                continue
            if self.ai_exclude and as_int(it.get("aiType"), 0) != 0:
                continue
            n += 1
        return n

    def crawl_keyword(self, keyword: str, pages: int, order: str, mode: str, s_mode: str,
                      max_works: int, *, deep: bool = False,
                      dry_run: bool = False) -> Dict[str, int]:
        combos = self._combos(order, s_mode, deep)
        cap_desc = f"{max_works} 个作品" if max_works > 0 else "不限"
        out(f"\n=== 关键词：{keyword}（{'深度模式，' if deep else ''}"
            f"{len(combos)} 种搜索组合，每种最多 {pages} 页，本次上限 {cap_desc}） ===")
        collected: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        related: List[str] = []
        total: Optional[int] = None
        combo_stats: List[Dict[str, Any]] = []
        exhausted = False
        run_keys: List[str] = []
        # 干跑时不截断，才能如实报出接口到底能给多少（不下载任何文件）
        cap = 0 if dry_run else max_works
        effective_cap = max_works if max_works > 0 else int(self.cfg.get("max_works_per_run") or 0) \
            if not dry_run else 0

        for label, o, sm in combos:
            if self.should_stop():
                out("  [停止] 用户请求停止，跳过剩余搜索组合")
                break
            key = SessionState.make_key(keyword, o, sm)
            run_keys.append(key)
            self.report(stage="combo", done=len(run_keys), total=len(combos), detail=label or key)
            if self.state is not None and key in self.state.exhausted and not self.force:
                out(f"  · 跳过已爬完的组合：{label or key}"
                    "（该组合上次已翻到接口尽头；要重爬请加 --force 或 --restart）")
                continue
            before = len(seen_ids)
            cstats: Dict[str, int] = {}
            # 跳跃探测：有列表级筛选时，先跳着采样定位"有可用内容"的深度，
            # 避免从第 1 页顺序翻却连续多页全被筛掉（实测"排除 AI"时前 8 页 480 条全是 AI）
            start_page = 1
            if self._has_list_filter() and pages > 1:
                found = self.probe_first_useful_page(keyword, o, sm)
                if found == -1:
                    out("  · 跳跃探测：采样范围内没有能通过当前筛选的作品"
                        "（该筛选条件在此关键词下几乎无命中，建议放宽筛选或换关键词）")
                elif found > 1:
                    start_page = found
                    out(f"  · 跳跃探测：从第 {found} 页开始翻（前面的页基本都被筛选条件排除）")
            hit_end, t, rel = self.crawl_one_combo(keyword, o, sm, pages, seen_ids, collected,
                                                   label=label if deep else "", stats=cstats,
                                                   start_page=start_page)
            if t:
                total = t
            if rel:
                related = rel
            returned = cstats.get("returned", 0)
            added = len(seen_ids) - before
            combo_stats.append({"label": label or "默认", "order": o, "s_mode": sm,
                                "added": added, "returned": returned, "exhausted": hit_end})
            # 组合间重叠自检：新增率极低就说明这个组合与前面的几乎重合，白翻了一趟。
            # 实测：tag_full / popular_male / popular_female 与「最新」新增率 0%；
            # 登录后 popular 更是直接返回空。
            if deep and returned >= 60 and (not hit_end) and added * 10 < returned:
                pct = added * 100.0 / returned
                out(f"    ⚠ 本组合新增率仅 {pct:.0f}%（{added}/{returned} 条与前序组合重合）"
                    "—— 与已爬组合大量重叠，可考虑用 --deep 之外的其它关键词来扩展覆盖")
            elif deep and returned == 0:
                out(f"    ⚠ 本组合接口返回 0 条（{ORDER_MAP.get(o, o)} 排序可能不可用）")
            if hit_end:
                exhausted = True
                if self.state is not None and not dry_run:
                    self.state.mark_exhausted(key)
                    self.state.save([k for k in run_keys if k not in self.state.exhausted], run_done=False)
            if effective_cap > 0 and self._passing_count(collected) >= effective_cap:
                out(f"  已达本次上限 {effective_cap} 个作品，停止追加组合"
                    + ("（想继续扩大请把 --limit 调大，或把 config.json 里的 "
                       "max_works_per_run 设为 0）" if not dry_run else ""))
                break

        if total is not None:
            note = ""
            if not self.client.cookie and not self.client.access_token:
                note = "（免登录接口每种排序仅提供靠前的一段，实测约 600 个/组合；登录后可翻到数千个）"
            out(f"  pixiv 报告命中约 {total} 个作品；本次实际取到 {len(collected)} 个{note}")
        if related:
            out(f"  相关标签：{'、'.join(related[:12])}")

        # ---- 先按「列表阶段就能判断的条件」筛选，再套用数量上限 ----
        # 顺序很重要：如果先按 --limit 截断，被筛掉的作品会白白占用名额，
        # 极端情况（--limit 5 + 只收 2024 年）会一个都下不到。
        #
        # 画师筛选也放在这里：搜索列表自带 userId，列表阶段筛掉就不会发详情请求，
        # 而且干跑（不进入详情阶段）报出的数量才真实。
        # 早期只写在 prepare_work 里，导致干跑显示"可用于下载 180 个"、
        # 实际一个都下不到，数量是假的（实测踩到）。
        if self.artist_ids:
            kept = [it for it in collected
                    if str(it.get("userId") or "").strip() in self.artist_ids]
            dropped = len(collected) - len(kept)
            if dropped:
                out(f"  画师筛选：{len(collected)} -> {len(kept)} 个作品"
                    f"（排除 {dropped} 个不属于指定画师的）")
                self.filter_stats["skipped_artist"] += dropped
            collected = kept

        if self.range.active:
            kept = [it for it in collected
                    if self.range.contains(parse_illust_datetime(it.get("createDate")))]
            dropped = len(collected) - len(kept)
            if dropped:
                out(f"  时间范围筛选：{len(collected)} -> {len(kept)} 个作品"
                    f"（排除 {dropped} 个不在 {self.range.describe()} 内的）")
                self.filter_stats["date"] += dropped
            collected = kept

        if self.ai_only or self.ai_exclude:
            def _is_ai(it: Dict[str, Any]) -> bool:
                return as_int(it.get("aiType"), 0) != 0
            kept = [it for it in collected
                    if (_is_ai(it) if self.ai_only else not _is_ai(it))]
            dropped = len(collected) - len(kept)
            if dropped:
                out(f"  AIGC 筛选：{len(collected)} -> {len(kept)} 个作品"
                    f"（排除 {dropped} 个{'非 AI 生成' if self.ai_only else 'AI 生成'}的）")
                self.filter_stats["skipped_ai"] = self.filter_stats.get("skipped_ai", 0) + dropped
            collected = kept

        if cap > 0 and len(collected) > cap:
            out(f"  按本次上限取前 {cap} 个（符合筛选条件的共有 {len(collected)} 个，"
                "想多收请调大 --limit）")
            collected = collected[:cap]

        if dry_run:
            out("\n  [dry-run 干跑] 只侦察，不下载任何文件：")
            out("    " + pad_display("搜索组合", 30) + pad_display("接口返回", 10, "right")
                + pad_display("新增", 8, "right") + pad_display("新增率", 8, "right") + "   状态")
            for cs in combo_stats:
                ret = cs.get("returned", 0)
                add = cs["added"]
                rate = f"{add * 100.0 / ret:.0f}%" if ret else "—"
                flag = "已到接口尽头" if cs["exhausted"] else ""
                if ret and add * 10 < ret:
                    flag = (flag + "　与已爬组合大量重叠").strip()
                out("    " + pad_display(cs["label"], 30) + pad_display(ret, 10, "right")
                    + pad_display(add, 8, "right") + pad_display(rate, 8, "right")
                    + "   " + flag)
            if len(combo_stats) > 1:
                sum_ret = sum(cs.get("returned", 0) for cs in combo_stats)
                uniq = len(seen_ids)
                if sum_ret:
                    dup = sum_ret - uniq
                    out(f"    组合间去重：各组合共返回 {sum_ret} 条，去重后唯一 {uniq} 条"
                        f"（重复 {dup} 条，重复率 {dup * 100.0 / sum_ret:.0f}%）")
                    out("    → 重复率高说明这些排序大量重合，加更多组合收益有限；"
                        "重复率低说明组合确实互补")
            real_cap = max_works if max_works > 0 else int(self.cfg.get("max_works_per_run") or 0)
            usable = len(collected) if real_cap <= 0 else min(len(collected), real_cap)
            out(f"  通过筛选、可用于下载的作品总数：{len(collected)}")
            if real_cap > 0 and len(collected) > real_cap:
                out(f"  但本次上限 max_works_per_run/--limit={real_cap} 会把它截到 {usable} 个"
                    "（改配置或加 --limit 可调整）")
            out(f"  因此一次运行最多可下载：{usable} 个作品")
            if (not deep) and exhausted:
                out("  提示：加 --deep 可自动组合多种排序，扩大可获取的作品数。")
            if total and total > len(collected):
                out(f"  注：pixiv 报告共有 {total} 个结果，其余部分网页接口拿不到"
                    + ("；配置登录凭据（cookie_phpsessid）或加 --deep 可再扩展一部分。"
                       if not self.client.logged_in else "。"))
            return {"downloaded": 0, "skipped": 0, "failed": 0, "pages": pages,
                    "works": len(collected), "candidates": len(collected),
                    # 供程序化调用方（自检/测试）核对组合重叠情况
                    "combos": combo_stats,
                    "combos_returned": sum(cs.get("returned", 0) for cs in combo_stats),
                    "unique": len(seen_ids)}

        if not collected:
            return {"downloaded": 0, "skipped": 0, "failed": 0, "pages": 0, "works": 0}

        query_keys = dedupe_keep_order([keyword] + related[:6])
        concurrency = max(1, int(self.cfg.get("concurrency") or 4))
        out(f"  开始处理 {len(collected)} 个作品（并发下载 {concurrency}）…")

        pending: List[Dict[str, Any]] = []
        guard = {"works": 0, "down": self.downloaded, "fail": self.failed}
        processed = 0          # 本次真正处理过的作品数（含已存在而登记的）
        hit_limit = False
        for it in collected:
            iid = str(it.get("id"))
            if cap > 0 and processed >= cap:
                hit_limit = True
                break
            already = self.known.get(iid, 0)
            brief_pages = int(it.get("pageCount") or 1)
            # 只有当索引里记录的最大页数已覆盖作品真实页数时才跳过；
            # 否则交给 prepare_work 按「磁盘实际缺页」逐页补齐
            if self.force or already < brief_pages:
                try:
                    detail, tasks = self.prepare_work(it, keyword, int(it.get("_rank") or 0),
                                                      str(it.get("_order") or order), query_keys)
                except CrawlError as exc:
                    self.failed += 1
                    out(f"  [warn] 作品 {iid} 处理失败：{exc}")
                    continue
                except Exception as exc:  # noqa: BLE001
                    self.failed += 1
                    out(f"  [warn] 作品 {iid} 处理异常：{type(exc).__name__}: {exc}")
                    continue
                if detail is not None:
                    missed = [t for t in tasks if t["need"]]
                    pending.extend(missed)
                    if missed and already:
                        out(f"  - 补齐缺页 {iid}：需要下载 {len(missed)} 页"
                            f"（索引记录 {already} 页 / 作品共 {len(tasks)} 页）")
                time.sleep(float(self.cfg.get("search_delay") or 0.3))
            else:
                if self.verbose:
                    out(f"  - 已存在（{already}/{brief_pages} 页），跳过下载 {iid}")
            # 无论是否新下载，都把该作品登记到当前关键词，保证 by_keyword 分类完整
            try:
                self._register_existing(iid, keyword, query_keys, int(it.get("_rank") or 0),
                                        str(it.get("_order") or order))
            except Exception as exc:  # noqa: BLE001
                if self.verbose:
                    out(f"  - [warn] 登记已存在作品 {iid} 失败：{type(exc).__name__}: {exc}")
            # 每攒够一批就落盘一次，避免中途中断丢索引
            if len(pending) >= 60:
                self._drain(pending, concurrency)
                pending = []
                self._flush_index()

            processed += 1   # 该作品已处理（下载或登记），用于 --limit 计数

            # 熔断保护：一段区间内大面积失败时停下来，避免把配额/触发风控后还硬跑
            guard["works"] += 1
            if guard["works"] % 10 == 0:
                got = self.downloaded - guard["down"]
                bad = self.failed - guard["fail"]
                if got + bad >= 6 and bad >= 4 and bad / max(1, got + bad) > 0.5:
                    out(f"\n  ✖ 最近 {got + bad} 次下载中有 {bad} 次失败，已中止本关键词的后续下载。")
                    out("    常见原因：登录态失效 / 被限流 / 网络或代理不稳。")
                    out("    已下载的内容与索引都已保存；建议先跑 selftest 确认凭据，再重跑本命令续爬。")
                    if self.state is not None:
                        self.state.save([k for k in run_keys if k not in self.state.exhausted], run_done=False)
                    break
                guard = {"works": guard["works"], "down": self.downloaded, "fail": self.failed}
        if pending:
            self._drain(pending, concurrency)
            self._flush_index()

        out(f"  本关键词完成：新下载 {self.downloaded} 张，跳过 {self.skipped}，失败 {self.failed}")
        if hit_limit:
            out(f"  已处理满本次上限 {cap} 个作品，剩余 {len(collected) - processed} 个留待下次"
                "（再跑一次会自动接着处理，已下载的不会重复下载）")
        fs = self.filter_stats
        if fs["date"] or fs["like"] or fs.get("unknown"):
            parts = []
            if fs["date"]:
                parts.append(f"时间范围外 {fs['date']} 个")
            if fs["like"]:
                parts.append(f"人气不达标 {fs['like']} 个")
            if fs.get("unknown"):
                parts.append(f"取不到计数而放行 {fs['unknown']} 个")
            out(f"  被筛选条件排除：{'、'.join(parts)}")
        return {
            "downloaded": self.downloaded, "skipped": self.skipped,
            "failed": self.failed, "pages": pages, "works": len(collected),
            "candidates": len(collected),
        }

    def crawl_artist(self, artist_id: str, *, name: str = "", max_works: int = 0,
                     pages: int = 1, dry_run: bool = False,
                     order: str = "date", s_mode: str = "s_tag") -> Dict[str, int]:
        """追更一个画师：拉取其全部作品 ID，只下载索引里还没有的那些。

        与关键词搜索的区别：
          * 不受"每种排序只给一段结果"的限制（画师作品列表是完整的）
          * 天然增量：已下载过的作品直接跳过，所以可以用定时任务反复跑
        """
        label = f"{name}（id={artist_id}）" if name else f"id={artist_id}"
        out(f"\n=== 追更画师：{label} ===")
        ids, names = self.client.artist_work_ids(artist_id)
        if not ids:
            out("  该画师没有公开作品，或接口返回为空（也可能是网络/权限问题）")
            return {"works": 0, "downloaded": 0, "skipped": 0, "failed": 0, "new": 0}
        display = names.get(artist_id) or name or artist_id
        known = self.known
        new_ids = [i for i in ids if i not in known]
        out(f"  作品总数 {len(ids)} 个；其中 {len(ids) - len(new_ids)} 个已在图库，"
            f"**新增 {len(new_ids)} 个**")
        if not new_ids:
            out("  没有新作品，无需下载。")
            return {"works": len(ids), "downloaded": 0, "skipped": 0, "failed": 0, "new": 0}

        if order == "old":
            new_ids = list(reversed(new_ids))
        cap = max_works if max_works > 0 else int(self.cfg.get("max_works_per_run") or 0)
        if dry_run:
            out("  [dry-run] 需要下载的作品 ID：")
            for wid in new_ids[:40]:
                out(f"    {wid}")
            if len(new_ids) > 40:
                out(f"    …（其余 {len(new_ids) - 40} 个）")
            if cap > 0 and len(new_ids) > cap:
                out(f"  注意：本次上限 {cap} 会把它截到 {cap} 个（--limit 或 max_works_per_run 控制）")
            return {"works": len(ids), "downloaded": 0, "skipped": 0, "failed": 0,
                    "new": len(new_ids)}

        targets = new_ids[:cap] if cap > 0 else new_ids
        query_keys = dedupe_keep_order([display, artist_id] + ([name] if name else []))
        concurrency = max(1, int(self.cfg.get("concurrency") or 4))
        out(f"  开始处理 {len(targets)} 个新作品（并发下载 {concurrency}）…")

        pending: List[Dict[str, Any]] = []
        processed = 0
        for rank, wid in enumerate(targets, 1):
            if self.should_stop():
                out(f"  [停止] 用户请求停止，已保存 {processed}/{len(targets)} 个作品的处理进度")
                break
            self.report(stage="artist", done=rank, total=len(targets), detail=wid)
            brief: Dict[str, Any] = {"id": wid, "title": "", "pageCount": 1,
                                     "xRestrict": 0, "illustType": 0}
            try:
                detail, tasks = self.prepare_work(brief, display, rank, order, query_keys)
            except CrawlError as exc:
                self.failed += 1
                out(f"  [warn] 作品 {wid} 处理失败：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                out(f"  [warn] 作品 {wid} 异常：{type(exc).__name__}: {exc}")
                continue
            if detail is not None:
                pending.extend([t for t in tasks if t["need"]])
            processed += 1
            if len(pending) >= 60:
                self._drain(pending, concurrency)
                pending = []
                self._flush_index()
            time.sleep(float(self.cfg.get("search_delay") or 0.3))
        if pending:
            self._drain(pending, concurrency)
        self._flush_index()
        out(f"  追更完成：新下载 {self.downloaded} 张，失败 {self.failed}"
            f"（本画师共处理 {processed} 个新作品）")
        return {"works": len(ids), "downloaded": self.downloaded, "skipped": self.skipped,
                "failed": self.failed, "new": len(new_ids)}

    def crawl_segmented(
        self,
        keyword: str,
        *,
        order: str = "date",
        mode: str = "all",
        s_mode: str = "s_tag",
        start: Optional[date] = None,
        end: Optional[date] = None,
        target: int = SEG_TARGET,
        pages_per_seg: int = SEG_PAGES_PER_SEG,
        on_progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """按时间段递归切分，把一个大关键词的**全部**结果收集下来。

        原理：单一搜索有翻页硬上限（实测约 6180 条/查询，第 104 页起返回空列表）。
        但 pixiv 网页搜索支持**服务端**日期筛选 scd/ecd，**每个时间段都有自己独立的
        翻页额度**。于是把时间范围递归二分，直到每段的结果数都低于上限，逐段取完。

        为什么用二分而不是"按月切"：结果在时间上极不均匀 —— 冷门年份整年可能不到
        5000 条（一次取完），热门月份可能上万条（必须再切）。二分能自动适配：
        只在"这段太多"时才继续分。745,201 条这种量级不需要为它单独写代码，
        54 万、100 万都是同一套逻辑，只是分的段数不同。

        返回统计信息；作品通过 collected/seen_ids 累积（与 crawl_one_combo 同一套）。
        """
        start = start or SEG_EPOCH
        end = end or datetime.now().date()
        if start > end:
            start, end = end, start

        stats: Dict[str, Any] = {
            "segments_done": 0, "segments_split": 0, "segments_skipped": 0,
            "segments_truncated": 0, "pages": 0, "unique": 0, "works": 0,
            "max_seg_works": 0, "start": date_to_scd(start), "end": date_to_scd(end),
            "target": target,
        }
        store = self.seg_state

        def walk(a: date, b: date, depth: int = 0) -> None:
            if self.should_stop():
                out("  [停止] 用户请求停止，全量分段截断（已完成的段已保存）")
                store.save()
                return
            scd, ecd = date_to_scd(a), date_to_scd(b)
            key = seg_key(keyword, scd, ecd, order, s_mode, mode)
            if store.is_done(key):
                stats["segments_skipped"] += 1
                return

            # 先只取第 1 页：拿该段的页数/总数（顺便复用这页数据，不浪费请求）
            try:
                items, extra = self.client.search_illusts(
                    keyword, 1, order=order, mode=mode, s_mode=s_mode, scd=scd, ecd=ecd)
            except RateLimited as exc:
                out(f"    [限流] {exc}")
                return
            total = as_int(extra.get("total"), 0)
            last_page = as_int(extra.get("last_page"), 0)

            # 拆分判据用 **lastPage** 而不是 total：
            # 实测发现 total 在宽区间下会退化（把"到 ecd 为止的累计量"报出来），
            # 例如 scd=2007-09-01&ecd=2026-09-29 报 68397，而同区间的
            # 两个子区间之和是 24154+68397 —— 父小于子，数学上不可能。
            # 但 lastPage 是准的：5947 条 -> lastPage=100（×60≈6000），
            # 199 条 -> lastPage=4。而且 lastPage 的上限就是 1000（约 6 万条），
            # 撞到 1000 就必须继续拆。所以用它做判据最稳。
            est_pages = last_page or max(1, (total + 59) // 60)
            hit_page_cap = last_page >= 1000
            # 段"够小"的判据同时看页数与预估条数，取更严格的那个
            page_limit = min(pages_per_seg, max(1, (target + 59) // 60))
            sub = split_span(a, b)
            if (hit_page_cap or est_pages > page_limit) and sub is not None:
                # 这段太热，一分为二再看
                stats["segments_split"] += 1
                if depth <= 1 or stats["segments_split"] % 20 == 0:
                    out(f"    ÷ 细分 [{scd} ~ {ecd}] "
                        f"{'页数撞上限' if hit_page_cap else f'约 {est_pages} 页'} "
                        f"total={total}（{sub[0][0]}~{sub[0][1]} / {sub[1][0]}~{sub[1][1]}）")
                walk(sub[0][0], sub[0][1], depth + 1)
                walk(sub[1][0], sub[1][1], depth + 1)
                return

            # 这段够小（或已经切到最小单位）：整段取完
            # 上面第 1 页已经取过了，这里把它算进结果，避免重复请求
            seg_seen: Set[str] = set()
            for it in items:
                iid = str(it.get("id") or "")
                if iid and iid not in self._seg_seen_ids:
                    it["_order"] = order
                    it["_s_mode"] = s_mode
                    it["_scd"] = scd
                    it["_ecd"] = ecd
                    it["_rank"] = len(self._seg_seen_ids)
                    self._seg_seen_ids.add(iid)
                    self._seg_collected.append(it)
                if iid:
                    seg_seen.add(iid)
            stats["pages"] += 1 if items else 0

            truncated = False
            if total > SEG_PAGE_CAP:
                truncated = True     # 单日都超过上限：无法保证取全
            elif len(items) == 60 and total > len(seg_seen):
                page = 2
                while page <= pages_per_seg:
                    try:
                        more, ex2 = self.client.search_illusts(
                            keyword, page, order=order, mode=mode, s_mode=s_mode,
                            scd=scd, ecd=ecd)
                    except RateLimited as exc:
                        out(f"    [限流] {exc}")
                        continue
                    if not more:
                        if total and len(seg_seen) < total * 0.98:
                            truncated = True
                        break
                    before = len(seg_seen)
                    for it in more:
                        iid = str(it.get("id") or "")
                        if not iid:
                            continue
                        seg_seen.add(iid)
                        if iid not in self._seg_seen_ids:
                            it["_order"] = order
                            it["_s_mode"] = s_mode
                            it["_scd"] = scd
                            it["_ecd"] = ecd
                            it["_rank"] = len(self._seg_seen_ids)
                            self._seg_seen_ids.add(iid)
                            self._seg_collected.append(it)
                    stats["pages"] += 1
                    if len(seg_seen) == before:
                        break
                    page += 1

            got = len([1 for i in seg_seen if True])
            if truncated:
                stats["segments_truncated"] += 1
                out(f"    ⚠ 段 [{scd} ~ {ecd}] 报告 {total} 条，接口只给到 {got} 条"
                    f"（已到翻页上限，这段拿不全）")
            else:
                store.mark_done(key, count=got)
                stats["segments_done"] += 1
            stats["unique"] = len(self._seg_seen_ids)
            stats["works"] = len(self._seg_collected)
            if on_progress:
                on_progress(stats, scd, ecd, total)

        out(f"\n=== 全量分段爬取：{keyword} ===")
        out(f"  时间范围 {date_to_scd(start)} ~ {date_to_scd(end)}"
            f"（{(end - start).days + 1} 天）")
        out(f"  每段目标 < {target} 条（接口单查询上限约 {SEG_PAGE_CAP} 条）")
        out(f"  排序 {ORDER_MAP.get(order, order)}／匹配 {S_MODE_MAP.get(s_mode, s_mode)}")
        if store.done:
            out(f"  已有 {len(store.done)} 段记录（已完成的不再重复请求，--restart 可清空）")
        walk(start, end)
        store.save()
        out(f"\n  分段统计：完成 {stats['segments_done']} 段，细分 {stats['segments_split']} 次，"
            f"跳过 {stats['segments_skipped']} 段（已爬过），共 {stats['pages']} 页请求")
        if stats["segments_truncated"]:
            out(f"  ⚠ 有 {stats['segments_truncated']} 段因单段超过接口上限而未能取全"
                "（这些是热门时段，单日作品数就超过上限）")
        out(f"  本关键词共收集到 {stats['works']} 个作品")
        return stats

    def crawl_artists(self, artist_ids: Sequence[str], *, max_works: int = 0,
                      dry_run: bool = False, order: str = "date",
                      s_mode: str = "s_tag") -> Dict[str, Any]:
        """按画师 ID 直接爬取（走 /ajax/user/{id}/profile/all，不走关键词搜索）。

        为什么这条路更好：
          * **不受单一查询约 6180 条的翻页上限**（画师作品列表是完整的，实测某画师
            632 个作品全部拿到）
          * **不受"AI 作品占满前排"影响** —— 关键词搜索时前排常被 AI 内容占满，
            按画师取则是该画师的全集
          * 天然增量：已下载过的作品直接跳过

        代价：需要逐个作品取详情（筛选分级/AI/人气也都在这一步做），
        所以比关键词搜索慢，但拿得全。
        """
        out(f"\n=== 按画师 ID 爬取：{len(artist_ids)} 位画师 ===")
        if self.r18_levels != R18_LEVELS or self.ai_only or self.ai_exclude:
            bits = []
            if self.r18_levels != R18_LEVELS:
                bits.append("分级=" + "、".join(R18_LEVEL_NAMES[x] for x in self.r18_levels))
            if self.ai_only:
                bits.append("只要 AI 生成")
            elif self.ai_exclude:
                bits.append("排除 AI 生成")
            out(f"  筛选条件：{'；'.join(bits)}（在取详情阶段逐作品判断）")
        total_new = total_down = total_fail = total_seen = 0
        per_artist: List[Dict[str, Any]] = []
        cap = max_works if max_works > 0 else int(self.cfg.get("max_works_per_run") or 0)

        for idx, aid in enumerate(artist_ids, 1):
            if self.should_stop():
                out("  [停止] 用户请求停止，跳过剩余画师")
                break
            out(f"\n--- [{idx}/{len(artist_ids)}] 画师 {aid} ---")
            self.report(stage="artist_scan", done=idx, total=len(artist_ids), detail=aid)
            ids, names = self.client.artist_work_ids(aid)
            if not ids:
                out("  该画师没有公开作品，或接口返回为空（也可能是网络/权限问题）")
                per_artist.append({"artist_id": aid, "works": 0, "new": 0, "downloaded": 0})
                continue
            display = names.get(aid) or aid
            known = self.known
            new_ids = [i for i in ids if i not in known]
            out(f"  {display}：作品共 {len(ids)} 个，其中 {len(ids) - len(new_ids)} 个已在图库，"
                f"新增待处理 {len(new_ids)} 个")
            total_seen += len(ids)
            if not new_ids:
                per_artist.append({"artist_id": aid, "works": len(ids), "new": 0, "downloaded": 0})
                continue
            if order == "old":
                new_ids = list(reversed(new_ids))
            # 本次总上限要跨画师累计，否则"每位画师都取 cap 个"会超
            if cap > 0:
                remain = cap - total_down
                if remain <= 0:
                    out(f"  已达本次上限 {cap} 个作品，跳过剩余画师")
                    break
                new_ids = new_ids[:remain]
            if dry_run:
                out(f"  [dry-run] 待下载 {len(new_ids)} 个，前几个 ID："
                    f"{'、'.join(new_ids[:12])}"
                    + (f"…（共 {len(new_ids)} 个）" if len(new_ids) > 12 else ""))
                per_artist.append({"artist_id": aid, "works": len(ids), "new": len(new_ids),
                                   "downloaded": 0})
                total_new += len(new_ids)
                continue

            query_keys = dedupe_keep_order([display, aid])
            concurrency = max(1, int(self.cfg.get("concurrency") or 4))
            pending: List[Dict[str, Any]] = []
            processed = 0
            skipped_before = self.skipped
            for rank, wid in enumerate(new_ids, 1):
                if self.should_stop():
                    out(f"  [停止] 用户请求停止，{display} 处理到 {processed}/{len(new_ids)}")
                    break
                self.report(stage="artist_dl", done=rank, total=len(new_ids), detail=wid)
                brief: Dict[str, Any] = {"id": wid, "title": "", "pageCount": 1,
                                         "xRestrict": 0, "illustType": 0}
                try:
                    detail, tasks = self.prepare_work(brief, display, rank, order, query_keys)
                except CrawlError as exc:
                    self.failed += 1
                    out(f"  [warn] 作品 {wid} 失败：{exc}")
                    continue
                except Exception as exc:  # noqa: BLE001
                    self.failed += 1
                    out(f"  [warn] 作品 {wid} 异常：{type(exc).__name__}: {exc}")
                    continue
                if detail is not None:
                    pending.extend([t for t in tasks if t["need"]])
                processed += 1
                if len(pending) >= 60:
                    self._drain(pending, concurrency)
                    pending = []
                    self._flush_index()
            if pending:
                self._drain(pending, concurrency)
            self._flush_index()
            got = self.downloaded
            total_new += len(new_ids)
            total_down += max(0, got)
            total_fail = self.failed
            out(f"  {display} 完成：处理 {processed} 个，本画师新增下载 "
                f"{max(0, got)} 张，按筛选跳过 {self.skipped - skipped_before} 张")
            per_artist.append({"artist_id": aid, "works": len(ids), "new": len(new_ids),
                               "downloaded": max(0, got)})
        out(f"\n  按画师爬取合计：画师作品 {total_seen} 个，待处理新增 {total_new} 个，"
            f"本次下载 {self.downloaded} 张，失败 {self.failed}")
        return {"works": total_seen, "new": total_new, "downloaded": self.downloaded,
                "failed": self.failed, "per_artist": per_artist}

    def crawl_bookmarks(self, user_id: str, *, rest: str = "show", tag: str = "",
                        max_works: int = 0, dry_run: bool = False,
                        order: str = "date") -> Dict[str, int]:
        """爬取某用户收藏夹里的作品（增量：已下载的自动跳过）。

        收藏简表自带 tags/aiType/xRestrict/pageCount/createDate，
        所以分级/AI/画师/时间/已下载都能在**列表阶段**过滤，比画师追更省详情请求。
        """
        known = self.known
        briefs, total = self.client.collect_bookmarks(
            user_id, rest=rest, tag=tag, max_works=0, log=out)
        if not briefs:
            out(f"  收藏夹（{user_id}）没有可用的作品（或接口返回为空）。")
            return {"works": 0, "downloaded": 0, "skipped": 0, "failed": 0, "new": 0}

        # 列表阶段筛选：重复 / 分级 / AI / 画师
        kept: List[Dict[str, Any]] = []
        for b in briefs:
            iid = str(b.get("id") or "")
            if not iid:
                continue
            if iid in known:
                continue
            xr = int(b.get("xRestrict") or 0)
            if self.r18_levels != R18_LEVELS and xr not in self.r18_levels:
                self.skipped_r18 += (xr == 1 or xr == 2)
                continue
            is_ai = int(b.get("aiType") or 0) != 0
            if self.ai_only and not is_ai:
                continue
            if self.ai_exclude and is_ai:
                continue
            if self.artist_ids and str(b.get("userId") or "") not in self.artist_ids:
                continue
            kept.append(b)
        new_ids = [str(b.get("id")) for b in kept]
        out(f"  收藏夹共 {total} 个作品；其中 {len(briefs) - len(new_ids)} 个已在图库/被筛选掉，"
            f"**待处理 {len(new_ids)} 个**")
        if not new_ids:
            out("  没有需要下载的作品。")
            return {"works": total, "downloaded": 0, "skipped": 0, "failed": 0, "new": 0}
        if order == "old":
            new_ids = list(reversed(new_ids))
        cap = max_works if max_works > 0 else int(self.cfg.get("max_works_per_run") or 0)
        if dry_run:
            out(f"  [dry-run] 待下载 {len(new_ids)} 个，前几个 ID："
                f"{'、'.join(new_ids[:12])}"
                + (f"…（共 {len(new_ids)} 个）" if len(new_ids) > 12 else ""))
            return {"works": total, "downloaded": 0, "skipped": 0, "failed": 0,
                    "new": len(new_ids)}

        targets = new_ids[:cap] if cap > 0 else new_ids
        query_keys = dedupe_keep_order([f"bookmarks:{user_id}", user_id])
        concurrency = max(1, int(self.cfg.get("concurrency") or 4))
        out(f"  开始处理 {len(targets)} 个新作品（并发下载 {concurrency}）…")
        pending: List[Dict[str, Any]] = []
        by_id = {str(b.get("id")): b for b in kept}
        processed = 0
        for rank, wid in enumerate(targets, 1):
            if self.should_stop():
                out(f"  [停止] 用户请求停止，已保存 {processed}/{len(targets)} 个作品的处理进度")
                break
            self.report(stage="bookmarks", done=rank, total=len(targets), detail=wid)
            b = by_id.get(wid) or {}
            brief = {"id": wid, "title": b.get("title") or "",
                     "pageCount": int(b.get("pageCount") or 1),
                     "xRestrict": int(b.get("xRestrict") or 0),
                     "illustType": b.get("illustType") or "illust"}
            try:
                detail, tasks = self.prepare_work(brief, f"bookmarks:{user_id}",
                                                  rank, order, query_keys)
            except CrawlError as exc:
                self.failed += 1
                out(f"  [warn] 作品 {wid} 处理失败：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                out(f"  [warn] 作品 {wid} 异常：{type(exc).__name__}: {exc}")
                continue
            if detail is not None:
                pending.extend([t for t in tasks if t["need"]])
            processed += 1
            if len(pending) >= 60:
                self._drain(pending, concurrency)
                pending = []
                self._flush_index()
            time.sleep(float(self.cfg.get("search_delay") or 0.3))
        if pending:
            self._drain(pending, concurrency)
        self._flush_index()
        out(f"  收藏夹完成：新下载 {self.downloaded} 张，失败 {self.failed}"
            f"（共处理 {processed} 个新作品）")
        return {"works": total, "downloaded": self.downloaded, "skipped": self.skipped,
                "failed": self.failed, "new": len(targets)}

    def _drain(self, tasks: List[Dict[str, Any]], concurrency: int) -> None:
        if not tasks:
            return
        with futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(self.download_task, tasks))

    def _flush_index(self) -> Tuple[int, int]:
        """先把待分类记录建成硬链接，再增量写入 records.jsonl。返回 (新增, 更新)。"""
        with self.rec_lock:
            if not self.new_records:
                return 0, 0
            batch = list(self.new_records)
            self.new_records.clear()
        try:
            stats = self.classify(batch)
            for k, v in stats.items():
                self.link_stats[k] = self.link_stats.get(k, 0) + int(v)
        except Exception as exc:  # noqa: BLE001
            out(f"    [warn] 分类目录生成异常：{type(exc).__name__}: {exc}")
        added, updated = self.lib.upsert_records(batch)
        for r in batch:
            self.lib.write_meta(r)
        for r in batch:
            iid = str(r.get("id"))
            self.known[iid] = max(self.known.get(iid, 0), int(r.get("page") or 0) + 1)
        if self.verbose and (added or updated):
            out(f"    [index] 已写入索引：新增 {added}，更新 {updated}")
        return added, updated

    def _register_existing(self, iid: str, keyword: str, query_keys: Sequence[str],
                           rank: int, order: str) -> None:
        """把已存在作品补登记到当前关键词（更新其 query/query_keys/rank）。"""
        try:
            detail = self.client.illust_detail(iid)
        except CrawlError:
            return
        if not detail:
            return
        tags = tags_from_detail(detail, self.stopwords, int(self.cfg.get("max_tags_per_work") or 8))
        urls = self.client.resolve_original_urls(iid, detail)
        author_dir = author_dir_name(str(detail.get("userName") or ""), str(detail.get("userId") or ""))
        recs = []
        for idx, u in enumerate(urls):
            ext = u.get("ext") or guess_ext(u["url"])
            dest = self.lib.originals / author_dir / f"{iid}_p{idx}{ext}"
            if not os.path.exists(native_path(dest)):
                continue
            rel = os.path.relpath(native_path(dest), native_path(self.lib.root))
            rec = build_record(
                illust_id=iid, page_index=idx, detail=detail, url=u["url"], ext=ext, path_rel=rel,
                bytes_size=file_size(dest), width=u.get("width") or detail.get("width"),
                height=u.get("height") or detail.get("height"), keyword=keyword, tags=tags,
                query_keys=query_keys, rank=rank, order=order,
            )
            recs.append(rec)
        with self.rec_lock:
            self.new_records.extend(recs)
        for t in tags:
            self.tag_counts[t["name"]] = self.tag_counts.get(t["name"], 0) + 1

    # ---- 收尾 ----

    def classify(self, recs: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, int]:
        """按标签/画师/关键词建立分类硬链接。recs 为 None 时用内存中的新记录。"""
        stats = {"tag_links": 0, "author_links": 0, "keyword_links": 0, "copies": 0}
        recs = list(recs) if recs is not None else list(self.new_records)
        if not recs:
            return stats
        pairs: List[Tuple[str, Path, Path]] = []  # (kind, src, dst)
        for r in recs:
            src = self.lib.root / str(r["file"])
            if not os.path.exists(native_path(src)):
                continue
            seq = (int(r.get("rank") or 0) * 100) + int(r.get("page") or 0)
            fname = link_name(r, seq)
            if parse_bool(self.cfg.get("make_tag_links"), True):
                for t in (r.get("tags") or [])[: int(self.cfg.get("max_tags_per_work") or 8)]:
                    pairs.append(("tag", src, self.lib.by_tag / tag_dir_name(str(t)) / fname))
            if parse_bool(self.cfg.get("make_author_links"), True):
                pairs.append(("author", src, self.lib.by_author /
                              author_dir_name(str(r.get("author") or ""), str(r.get("author_id") or "")) / fname))
            if parse_bool(self.cfg.get("make_keyword_links"), True):
                for k in (r.get("query_keys") or ([r.get("query")] if r.get("query") else [])):
                    pairs.append(("keyword", src, self.lib.by_keyword / kw_key(str(k)) / fname))
        for kind, src, dst in pairs:
            d = dst
            n = 1
            # 同名不同内容时加序号，避免互相覆盖
            while os.path.exists(native_path(d)) and file_size(d) != file_size(src):
                d = dst.with_name(f"{dst.stem}_{n}{dst.suffix}")
                n += 1
            try:
                how = link_or_copy(src, d)
            except CrawlError as exc:
                out(f"    [warn] {exc}")
                continue
            if how == "copy":
                stats["copies"] += 1
            if how in ("hardlink", "copy"):
                stats[f"{kind}_links"] = stats.get(f"{kind}_links", 0) + 1
        return stats

    def cleanup_stale_links(self, recs: Optional[Sequence[Dict[str, Any]]] = None) -> int:
        """清理分类目录里的失效硬链接。

        典型场景：同一作品换了关键词/排名后重新爬取，链接文件名里的序号会变，
        旧链接就会成为孤儿（例如 ugoira 从 .zip 链接改成封面 .jpg 链接之后）。

        匹配时按"作品ID + 页码"与当前索引核对，并允许两种合法文件名：
          * 记录 file 字段的扩展名
          * 封面帧的 ..._poster.jpg 变体（ugoira 的分类链接指向封面）
        只删除确实已无对应记录的链接，且只在本图库的 by_* 目录内操作。
        """
        allrecs = list(recs if recs is not None else self.lib.load_records())
        # 只认"当前索引真正指向的那个文件"：按 basename + 扩展名精确比对。
        # 早期版本把所有历史扩展名都放行，导致 ugoira 从 .zip 改成封面 .jpg 之后
        # 旧的 .zip 链接永远清不掉（实测踩到）。
        allowed: Dict[Tuple[str, int], Set[str]] = {}
        for r in allrecs:
            key = (str(r.get("id")), int(r.get("page") or 0))
            base = os.path.basename(str(r.get("file") or "")).lower()
            if base:
                allowed.setdefault(key, set()).add(base)
            anim = os.path.basename(str(r.get("animated_file") or "")).lower()
            if anim:
                allowed[key].add(anim)
            poster = os.path.basename(str(r.get("poster") or "")).lower()
            if poster:
                allowed[key].add(poster)
        removed = 0
        for base_dir in (self.lib.by_tag, self.lib.by_author, self.lib.by_keyword):
            if not os.path.isdir(native_path(base_dir)):
                continue
            for root, _dirs, files in os.walk(native_path(base_dir)):
                for fname in files:
                    if fname.endswith(".part"):
                        continue
                    m = LINK_NAME_RE.search(fname)
                    if not m:
                        continue
                    key = (m.group(1), int(m.group(2)))
                    if key in allowed and fname.lower() in allowed[key]:
                        continue
                    try:
                        os.remove(os.path.join(root, fname))
                        removed += 1
                    except OSError:
                        pass
        return removed

    def finalize(self, *, rebuild: bool = True) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "added": 0, "updated": 0,
            "downloaded": self.downloaded, "skipped": self.skipped, "failed": self.failed,
            "tag_links": 0, "author_links": 0, "keyword_links": 0, "copies": 0,
        }
        added, updated = self._flush_index()
        info["added"], info["updated"] = added, updated
        info.update(self.link_stats)
        info["downloaded"] = self.downloaded
        info["skipped"] = self.skipped
        info["failed"] = self.failed
        info["skipped_r18"] = self.skipped_r18
        info["skipped_r18g"] = self.skipped_r18g
        info["skipped_ugoira"] = self.skipped_ugoira
        if rebuild:
            allrecs = self.lib.load_records()
            self.lib.export_csv(allrecs)
            self.lib.export_markdown(allrecs)
            self.lib.build_sqlite(allrecs)
            info["total_records"] = len(allrecs)
        else:
            info["total_records"] = len(self.lib.load_records())
        return info


# --------------------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------------------


def resolve_credentials(cfg: Dict[str, Any], args: Optional[argparse.Namespace] = None) -> None:
    """按优先级把凭据合并进 cfg（就地修改）。

    优先级（后面的覆盖前面的）：
        config.json 里的值  <  用户凭据库  <  环境变量  <  命令行参数
    这样用户既可以把凭据放在共享配置里，也可以用只对自己可见的凭据库，
    还能在 CI 之类的场景用环境变量注入。
    """
    store = load_credential_store()
    if store.get("refresh_token"):
        cfg["refresh_token"] = str(store["refresh_token"])
    if store.get("phpsessid"):
        cfg["cookie_phpsessid"] = str(store["phpsessid"])

    env_token = os.environ.get(ENV_REFRESH_TOKEN, "").strip()
    if env_token:
        cfg["refresh_token"] = env_token
    env_sess = os.environ.get(ENV_PHPSESSID, "").strip()
    if env_sess:
        cfg["cookie_phpsessid"] = env_sess

    if args is not None:
        if getattr(args, "refresh_token", None):
            cfg["refresh_token"] = args.refresh_token
        if getattr(args, "cookie", None):
            cfg["cookie_phpsessid"] = args.cookie


def credentials_source(cfg: Dict[str, Any], args: Optional[argparse.Namespace] = None) -> str:
    """说明当前凭据是从哪儿来的（给用户看，避免"我明明填了却没生效"）。"""
    if args is not None:
        if getattr(args, "refresh_token", None):
            return "命令行参数 --refresh-token"
        if getattr(args, "cookie", None):
            return "命令行参数 --cookie"
    if os.environ.get(ENV_REFRESH_TOKEN, "").strip() or os.environ.get(ENV_PHPSESSID, "").strip():
        return "环境变量"
    store = load_credential_store()
    if store.get("refresh_token"):
        return f"凭据库 {CRED_FILE}"
    if store.get("phpsessid"):
        return f"凭据库 {CRED_FILE}"
    return "config.json"


def prepare_cfg(args: argparse.Namespace, program_dir: Path) -> Tuple[Dict[str, Any], Library]:
    cfg, cfg_path = load_config(program_dir, getattr(args, "config", None))
    # 命令行覆盖
    if getattr(args, "out", None):
        cfg["output_dir"] = args.out
    if getattr(args, "cookie", None):
        cfg["cookie_phpsessid"] = args.cookie
    if getattr(args, "refresh_token", None):
        cfg["refresh_token"] = args.refresh_token
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "insecure", False):
        cfg["verify_ssl"] = False
    if getattr(args, "concurrency", None):
        cfg["concurrency"] = args.concurrency
    if getattr(args, "rps", None):
        cfg["requests_per_second"] = args.rps
    if getattr(args, "timeout", None):
        cfg["timeout"] = args.timeout
    if getattr(args, "keep_r18", False):
        cfg["skip_r18"] = False
    if getattr(args, "keep_r18g", False):
        cfg["skip_r18"] = False     # R-18G 属于 R-18 的子集，放开 R-18 才有意义
        cfg["skip_r18g"] = False
    for flag, key in (("date_from", "date_from"), ("date_to", "date_to")):
        val = getattr(args, flag, None)
        if val is not None:
            cfg[key] = val
    if getattr(args, "min_likes", None) is not None:
        cfg["min_likes"] = args.min_likes
    if getattr(args, "min_bookmarks", None) is not None:
        cfg["min_bookmarks"] = args.min_bookmarks
    if getattr(args, "keep_ugoira", False):
        cfg["skip_ugoira"] = False
    if getattr(args, "ugoira_format", None):
        cfg["ugoira_format"] = args.ugoira_format
    if getattr(args, "drop_ugoira_zip", False):
        cfg["ugoira_keep_zip"] = False
    if getattr(args, "max_pages_per_work", None) is not None:
        cfg["max_pages_per_work"] = args.max_pages_per_work
    outdir = Path(str(cfg["output_dir"])).expanduser()
    if not outdir.is_absolute():
        outdir = (program_dir / outdir).resolve()
    cfg["output_dir"] = str(outdir)
    # 凭据来源优先级：config.json < 凭据库 < 环境变量 < 命令行
    resolve_credentials(cfg, args)
    if getattr(args, "verbose", False):
        out(f"[i] 程序目录：{program_dir}")
        out(f"[i] 配置文件：{cfg_path if cfg_path else '（未找到，使用内置默认值）'}")
        out(f"[i] 图库目录：{outdir}")
    lib = Library(outdir)
    lib.ensure()
    return cfg, lib


def parse_r18_levels(text: Any) -> Optional[List[int]]:
    """解析命令行 --r18 的取值 -> 级别列表。

    接受的写法（逗号分隔，可混用）：
        0 / all-ages / allages / 全年龄        -> 全年龄
        1 / r18 / R-18                        -> R-18
        2 / r18g / R-18G / g                  -> R-18G
        all                                   -> 三者都要
        none / off / ""                       -> 都不要
    例：--r18 0,1   全年龄 + R-18
        --r18 2     只要 R-18G
    """
    if text is None:
        return None                      # 未指定：沿用配置
    s = str(text).strip().lower()
    if s in ("", "none", "off", "no", "无"):
        return []
    if s in ("all", "全部", "都要"):
        return list(R18_LEVELS)
    alias = {
        "0": 0, "all-ages": 0, "allages": 0, "all_age": 0, "safe": 0, "全年龄": 0,
        "1": 1, "r18": 1, "r-18": 1, "r18a": 1, "一般": 1,
        "2": 2, "r18g": 2, "r-18g": 2, "g": 2, "grotesque": 2, "猎奇": 2,
    }
    out_levels: List[int] = []
    for part in re.split(r"[,，、+\s]+", s):
        part = part.strip()
        if not part:
            continue
        if part not in alias:
            raise ValueError(f"看不懂的分级写法：{part!r}（可用 0/1/2、all-ages/r18/r18g、"
                            "all、none，逗号分隔）")
        lvl = alias[part]
        if lvl not in out_levels:
            out_levels.append(lvl)
    return sorted(out_levels)


def cmd_estimate(args: argparse.Namespace, program_dir: Path) -> int:
    """预估命令：先算量级，再决定要不要真爬。"""
    cfg, lib = prepare_cfg(args, program_dir)
    keyword = (args.keyword or "").strip()
    if not keyword and (args.interactive or sys.stdin.isatty()):
        try:
            keyword = input("要预估的关键词> ").strip()
        except (EOFError, KeyboardInterrupt):
            keyword = ""
    if not keyword:
        out("用法：estimate <关键词>　例如 python pixiv_crawler.py estimate 初音ミク")
        return 2

    levels = parse_r18_levels(getattr(args, "r18_levels", None))
    if levels is not None:
        server_mode, allowed = r18_plan(levels)
        if not allowed:
            out("[错误] --r18 none 表示什么分级都不要，那就不用预估了。")
            return 2
        cfg["mode"] = server_mode
        cfg["skip_r18"] = False
        cfg["skip_r18g"] = False
        out(f"[i] 内容分级：收 {'、'.join(R18_LEVEL_NAMES[x] for x in allowed)}"
            f"（服务器 mode={server_mode}）")

    order = str(getattr(args, "order", None) or cfg.get("order") or "date")
    mode = str(getattr(args, "mode", None) or cfg.get("mode") or "all")
    s_mode = str(getattr(args, "s_mode", None) or cfg.get("s_mode") or "s_tag")
    probe = int(getattr(args, "probe_requests", 0) or 40)
    work_mb = getattr(args, "work_mb", None)
    wb = (float(work_mb) * 1048576) if work_mb else measure_local_work_bytes(lib)
    conc = int(getattr(args, "concurrency", 0) or 0)
    thr = float(getattr(args, "throughput", 0) or DEFAULT_THROUGHPUT)

    out("=" * 68)
    out(f"预估：关键词「{keyword}」全部爬下来要多少")
    out("=" * 68)
    if wb:
        note = "（本机图库实测均值）" if not work_mb else "（你指定的值）"
        out(f"每作品平均体积：{wb / 1048576:.2f} MB {note}")
    else:
        out(f"每作品平均体积：{DEFAULT_WORK_MB} MB（图库样本不足，用默认值）")
    out(f"最多发 {probe} 次探测请求，不下载任何图片\n")

    try:
        est = estimate_crawl(cfg, keyword=keyword, order=order, mode=mode, s_mode=s_mode,
                             max_requests=probe, work_bytes=wb,
                             concurrency=conc, throughput_mb=thr,
                             log=lambda m: out(f"  {m}"))
    except KeyboardInterrupt:
        out("\n[!] 已中断")
        return 1
    try:
        free = shutil.disk_usage(native_path(lib.root)).free
    except OSError:
        free = 0
    out("")
    for line in estimate_summary(est, disk_free=free):
        out("  " + line)
    if est.get("per_year"):
        out("")
        out("  各年份作品数（按年累加，实测精度 ±0.1%）：")
        for y, n in est["per_year"].items():
            out(f"    {y} 年：{n}")
    out("")
    out("提示：真正的全量爬取加 --full，例如：")
    out(f"  python pixiv_crawler.py crawl {keyword} --full")
    return 0


def cmd_crawl_bookmarks(args: argparse.Namespace, program_dir: Path,
                        cfg: Dict[str, Any], lib: Library) -> int:
    """收藏夹模式入口：--from-bookmarks <用户ID>（可选 --bookmark-rest hide / --bookmark-tag）。"""
    order = str(getattr(args, "order", None) or cfg.get("order") or "date")
    max_works = int(getattr(args, "limit", 0) or cfg.get("max_works_per_run") or 0)
    try:
        levels = parse_r18_levels(getattr(args, "r18_levels", None))
    except ValueError as exc:
        out(f"[错误] {exc}")
        return 2
    ai_mode = str(getattr(args, "ai_mode", None) or cfg.get("ai_mode") or "all")
    uids = list(getattr(args, "from_bookmarks", None) or [])
    out("=" * 68)
    out(f"收藏夹模式：用户 {uids[0]}"
        + ("（公开收藏）" if str(getattr(args, "bookmark_rest", "show")) != "hide" else "（私密收藏）"))
    out("=" * 68)
    run_crawl(
        cfg, lib, [],
        order=order, mode="all", s_mode="s_tag",
        max_works=max_works, force=bool(getattr(args, "force", False)),
        deep=False, dry_run=bool(getattr(args, "dry_run", False)),
        deep_all=False, restart=bool(getattr(args, "restart", False)),
        r18_levels=levels, ai_mode=ai_mode,
        artist_ids=getattr(args, "artist_ids", None),
        scope="bookmarks",
        bookmark_uids=uids,
        bookmark_rest=str(getattr(args, "bookmark_rest", None) or "show"),
        bookmark_tag=str(getattr(args, "bookmark_tag", None) or ""),
    )
    return 0


def cmd_crawl(args: argparse.Namespace, program_dir: Path) -> int:
    cfg, lib = prepare_cfg(args, program_dir)
    keywords: List[str] = list(args.keywords or [])
    # 收藏夹模式：不需要关键词（用 --from-bookmarks <用户ID> 指定）
    if getattr(args, "from_bookmarks", None) or \
            str(getattr(args, "scope", None) or "") == "bookmarks":
        fm = getattr(args, "from_bookmarks", None)
        if not fm:
            out("[!] 收藏夹模式需要指定用户 ID：--from-bookmarks <用户ID>")
            return 2
        return cmd_crawl_bookmarks(args, program_dir, cfg, lib)
    if args.interactive or not keywords:
        out("\n请输入要爬取的关键词（多个关键词用空格分隔，直接回车结束）：")
        try:
            line = input("关键词> ").strip()
        except (EOFError, KeyboardInterrupt):
            line = ""
        if line:
            keywords = [w for w in re.split(r"[\s,，]+", line) if w]
    if not keywords:
        out("没有关键词，退出。")
        return 0
    order = str(getattr(args, "order", None) or cfg.get("order") or "date")
    mode = str(getattr(args, "mode", None) or cfg.get("mode") or "all")
    s_mode = str(getattr(args, "s_mode", None) or cfg.get("s_mode") or "s_tag")
    pages = int(getattr(args, "pages", 0) or cfg.get("pages") or 1)
    max_works = int(getattr(args, "limit", 0) or cfg.get("max_works_per_run") or 0)
    try:
        levels = parse_r18_levels(getattr(args, "r18_levels", None))
    except ValueError as exc:
        out(f"[错误] {exc}")
        return 2
    ai_mode = str(getattr(args, "ai_mode", None) or cfg.get("ai_mode") or "all")

    if getattr(args, "full", False):
        # 全量模式：按时间段递归切分，突破单一查询的翻页上限
        start = parse_seg_date(getattr(args, "full_from", None))
        end = parse_seg_date(getattr(args, "full_to", None))
        target = int(getattr(args, "segment_target", 0) or SEG_TARGET)
        out("=" * 68)
        out("全量模式：按时间段递归切分，尽量把该关键词的结果全部取下来")
        out("=" * 68)
        out(f"[i] 先做一次预估（约 {SEG_ESTIMATE_REQUESTS} 次探测请求），"
            "让你知道这次的量级…")
        est = estimate_crawl(cfg, keyword=keywords[0], order=order, mode=mode, s_mode=s_mode,
                             start=start, end=end, target=target,
                             max_requests=SEG_ESTIMATE_REQUESTS, log=lambda m: out(f"  {m}"))
        try:
            free = shutil.disk_usage(native_path(lib.root)).free
        except OSError:
            free = 0
        for line in estimate_summary(est, disk_free=free):
            out("  " + line)
        if est.get("disk_bytes", 0) > free:
            out("\n[!] 磁盘空间不足，已停止。请先换图库位置（「图库位置」页）或缩小时间范围"
                "（--full-from / --full-to）。")
            return 1
        return run_full(cfg, lib, keywords, order=order, mode=mode, s_mode=s_mode,
                        start=start, end=end, target=target,
                        force=bool(getattr(args, "force", False)),
                        max_works=max_works,
                        r18_levels=levels, ai_mode=ai_mode,
                        artist_ids=getattr(args, "artist_ids", None))

    info = run_crawl(
        cfg, lib, keywords, pages=pages, order=order, mode=mode, s_mode=s_mode,
        max_works=max_works, force=bool(getattr(args, "force", False)),
        deep=bool(getattr(args, "deep", False)), dry_run=bool(getattr(args, "dry_run", False)),
        deep_all=bool(getattr(args, "deep_all", False)), restart=bool(getattr(args, "restart", False)),
        r18_levels=levels, ai_mode=ai_mode,
        artist_ids=getattr(args, "artist_ids", None),
        scope=str(getattr(args, "scope", None) or "auto"),
        bookmark_uids=(getattr(args, "from_bookmarks", None) or [None]
                       if getattr(args, "scope", None) == "bookmarks" else None),
        bookmark_rest=str(getattr(args, "bookmark_rest", None) or "show"),
        bookmark_tag=str(getattr(args, "bookmark_tag", None) or ""),
    )
    return 0


def run_full(
    cfg: Dict[str, Any],
    lib: Library,
    keywords: Sequence[str],
    *,
    order: str = "date",
    mode: str = "all",
    s_mode: str = "s_tag",
    start: Any = None,
    end: Any = None,
    target: int = SEG_TARGET,
    force: bool = False,
    max_works: int = 0,
    r18_levels: Optional[Sequence[int]] = None,
    ai_mode: str = "all",
    log=out,
) -> int:
    """全量模式主流程：分段收集 -> 下载 -> 写索引。

    与普通 crawl 的区别：作品列表来自 crawl_segmented（按时间段切分，能突破
    单一查询约 6180 条的上限），而不是 crawl_keyword（单一搜索）。
    """
    session = CrawlSession(cfg, lib, verbose=True)
    session.force = force
    if r18_levels is not None:
        server_mode, allowed = r18_plan(r18_levels)
        mode = server_mode
        session.r18_levels = tuple(allowed)
        if not allowed:
            log("[!] --r18 没有选中任何分级，等于什么都不爬。")
            return 1
        session.cfg["skip_r18"] = False
        session.cfg["skip_r18g"] = False
    ai_mode = (ai_mode or "all").lower()
    session.ai_only = ai_mode == "only"
    session.ai_exclude = ai_mode == "exclude"
    if ai_mode != "all":
        log(f"[i] AIGC 过滤：{'只收 AI 生成' if session.ai_only else '排除 AI 生成'}")
    session.known = lib.known_ids_fast()
    started = time.time()

    # 磁盘检查：全量动辄几百 GB ~ 几 TB，写满盘会让整个图库处于半损坏状态。
    # 这里只看"当前剩余空间"，不做精确预测（精确预测由 estimate 负责）。
    try:
        free_before = shutil.disk_usage(native_path(lib.root)).free
        if free_before < 2 * 1073741824:
            log(f"[!] 图库所在磁盘只剩 {format_size(free_before)}，不足以做全量爬取。"
                "请先换到更大的磁盘（图形界面「图库位置」页可切换），或缩小时间范围。")
            return 1
        log(f"[i] 图库所在磁盘剩余 {format_size(free_before)}")
    except OSError:
        pass

    for kw in keywords:
        session._seg_seen_ids.clear()
        session._seg_collected.clear()
        try:
            stats = session.crawl_segmented(kw, order=order, mode=mode, s_mode=s_mode,
                                            start=start, end=end, target=target)
        except KeyboardInterrupt:
            log("\n[!] 被中断。已完成的段已记录，下次运行会接着爬（已下载的图会自动跳过）。")
            break
        works = session._seg_collected
        log(f"\n[i] 开始处理 {len(works)} 个作品的下载…")
        # 分段收集到的条目要按作品去重（同一作品可能在多个段里出现）
        by_id: Dict[str, Dict[str, Any]] = {}
        for it in works:
            iid = str(it.get("id") or "")
            if iid and iid not in by_id:
                by_id[iid] = it
        # --limit 也要在全量模式下生效，否则想"小范围试跑"时会失控下载
        # （实测踩到：加了 --limit 3 但仍然下了 234 张）
        cap = max_works if max_works > 0 else int(cfg.get("max_works_per_run") or 0)
        if cap > 0 and len(by_id) > cap:
            log(f"[i] 本次上限 {cap} 个作品，从 {len(by_id)} 个里取前 {cap} 个"
                "（想全量请把 --limit / max_works_per_run 设为 0）")
            by_id = dict(list(by_id.items())[:cap])
        concurrency = max(1, int(cfg.get("concurrency") or 4))
        pending: List[Dict[str, Any]] = []
        processed = 0
        for iid, brief in by_id.items():
            try:
                detail, tasks = session.prepare_work(brief, kw, 0, order, [kw])
            except CrawlError as exc:
                session.failed += 1
                log(f"  [warn] 作品 {iid} 处理失败：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                session.failed += 1
                log(f"  [warn] 作品 {iid} 异常：{type(exc).__name__}: {exc}")
                continue
            if detail is not None:
                pending.extend([t for t in tasks if t["need"]])
            processed += 1
            if len(pending) >= 60:
                session._drain(pending, concurrency)
                pending = []
                session._flush_index()
            if processed % 200 == 0:
                log(f"  [进度] 已处理 {processed}/{len(by_id)} 个作品，"
                    f"已下载 {session.downloaded} 张，用时 {(time.time()-started)/60:.1f} 分钟")
        if pending:
            session._drain(pending, concurrency)
        session._flush_index()
        log(f"[i] 「{kw}」完成：分段 {stats.get('segments_done')} 段，"
            f"处理 {processed} 个作品，新下载 {session.downloaded} 张，失败 {session.failed}")

    log("\n[i] 整理分类目录与索引…")
    info = session.finalize(rebuild=True)
    st = session.http.stats
    session.dry_run = False
    finalize_run_record(session, lib, mode="full", keywords=keywords,
                        params={"order": order, "mode": mode, "s_mode": s_mode,
                                "max_works": max_works,
                                "seg_from": date_to_scd(start) if start else "",
                                "seg_to": date_to_scd(end) if end else "",
                                "target": target},
                        result=info, started=started, log=log)
    log("\n========== 全量结果 ==========")
    log(f"新下载图片 {info.get('downloaded', 0)} 张，图库共 {info.get('total_records', 0)} 条记录")
    log(f"分段完成 {len(session.seg_state.done)} 段（记录在 {lib.index_dir / 'segments.json'}）")
    log(f"网络请求 {st['requests']} 次，传输 {format_size(st['bytes'])}"
        + (f"，限流 {st['rate_limited']} 次" if st.get("rate_limited") else "")
        + f"，耗时 {(time.time()-started)/3600:.1f} 小时")
    log("想接着爬：再跑一次同样的命令即可（已完成的段会跳过、已下载的图会跳过）。")
    return 0


def run_crawl(
    cfg: Dict[str, Any],
    lib: Library,
    keywords: Sequence[str],
    *,
    pages: int = 1,
    order: str = "date",
    mode: str = "all",
    s_mode: str = "s_tag",
    max_works: int = 0,
    force: bool = False,
    deep: bool = False,
    deep_all: bool = False,
    dry_run: bool = False,
    restart: bool = False,
    r18_levels: Optional[Sequence[int]] = None,
    ai_mode: str = "all",
    artist_ids: Optional[Sequence[str]] = None,
    scope: str = "auto",
    segmented: bool = False,
    seg_start: Any = None,
    seg_end: Any = None,
    bookmark_uids: Optional[Sequence[str]] = None,
    bookmark_rest: str = "show",
    bookmark_tag: str = "",
    stop_event: Optional[Any] = None,
    progress_cb: Optional[Any] = None,
    log=out,
) -> Dict[str, Any]:
    """爬取主流程（命令行与图形界面共用）。log 可换成 GUI 的日志函数。"""
    session = CrawlSession(cfg, lib, verbose=True)
    started = time.time()
    if stop_event is not None:
        session.stop_event = stop_event
    if progress_cb is not None:
        session.progress_cb = progress_cb
    session.force = force
    session.deep_all = deep_all
    session.dry_run = dry_run
    started = time.time()
    # 分级：用户勾选的级别集合决定"服务器 mode"与"本地精筛"
    if r18_levels is not None:
        server_mode, allowed = r18_plan(r18_levels)
        mode = server_mode
        session.r18_levels = tuple(allowed)
        if not allowed:
            log("[!] 没有勾选任何内容分级（全年龄 / R-18 / R-18G），"
                "等于什么都不爬。请至少勾选一个。")
            return {"downloaded": 0, "skipped": 0, "failed": 0, "aborted": "no_r18_level"}
        # 用户显式勾选了就由 r18_levels 独占决定，屏蔽旧的 skip_r18/skip_r18g，
        # 否则"只收 R-18G"会被配置里的 skip_r18=true 再过滤一次，变成什么都收不到
        session.cfg["skip_r18"] = False
        session.cfg["skip_r18g"] = False
        log(f"[i] 内容分级：收 {'、'.join(R18_LEVEL_NAMES[x] for x in allowed)}"
            f"（服务器搜索 mode={server_mode}，其余在本地精筛）")
    # AIGC
    ai_mode = (ai_mode or "all").lower()
    session.ai_only = ai_mode == "only"
    session.ai_exclude = ai_mode == "exclude"
    if ai_mode != "all":
        log(f"[i] AIGC 过滤：{'只收 AI 生成' if session.ai_only else '排除 AI 生成'}"
            "（搜索列表自带 aiType，不额外消耗请求）")
    # 画师筛选：同样在列表阶段就能判断，不额外消耗请求
    if artist_ids:
        aids = {normalize_artist_id(a) for a in artist_ids}
        aids.discard("")
        bad = [a for a in artist_ids if str(a).strip() and not normalize_artist_id(a)]
        if bad:
            log(f"[!] 看不懂的画师 ID：{'、'.join(str(b) for b in bad)}"
                "（支持纯数字 ID 或 https://www.pixiv.net/users/12345）")
        if aids:
            session.artist_ids = aids
            log(f"[i] 画师筛选：只收 {len(aids)} 位画师的作品 "
                f"（{'、'.join(sorted(aids))}）")
            log("    提示：若想拿某位画师的**全部**作品（不受关键词与翻页上限限制），"
                "用 follow 命令更合适：python pixiv_crawler.py follow add <画师ID> && follow sync")

    # ---- 检索范围分发：只按画师ID 时走画师作品接口（不受翻页上限、不受 AI 占位影响）----
    scope = (scope or "auto").lower()
    if scope == "auto":
        scope = "artist" if (session.artist_ids and not keywords) else "keyword"
    if scope == "bookmarks":
        uid = str((bookmark_uids or [""])[0]).strip() if bookmark_uids else ""
        if not uid:
            log("[!] 检索范围选了「收藏夹」，但没有填用户 ID。")
            return {"downloaded": 0, "skipped": 0, "failed": 0, "aborted": "no_bookmark_uid"}
        if dry_run:
            log("[i] 收藏夹简表自带标签/分级/AI，干跑就能看到筛选后的真实数字")
        session.known = lib.known_ids_fast()
        stats = session.crawl_bookmarks(uid, rest=bookmark_rest or "show",
                                        tag=bookmark_tag or "", max_works=max_works,
                                        dry_run=dry_run, order=order)
        if not dry_run:
            log("\n[i] 整理分类目录与索引…")
            info = session.finalize(rebuild=True)
            log(f"\n新下载图片 {info.get('downloaded', 0)} 张，"
                f"图库共 {info.get('total_records', 0)} 条记录")
            stats.update({"added": info.get("added", 0), "updated": info.get("updated", 0),
                          "total_records": info.get("total_records", 0)})
            finalize_run_record(session, lib, mode="bookmarks",
                                keywords=[f"bookmarks:{uid}"],
                                params={"rest": bookmark_rest or "show",
                                        "max_works": max_works,
                                        "bookmark_tag": bookmark_tag or ""},
                                result=stats, started=started, log=log)
        return stats
    if scope == "artist":
        if not session.artist_ids:
            log("[!] 检索范围选了「画师ID」，但没有填任何画师 ID。")
            return {"downloaded": 0, "skipped": 0, "failed": 0, "aborted": "no_artist"}
        if dry_run:
            log("[i] 画师模式是逐作品取详情，干跑只列出待下载的作品 ID，不做筛选判断")
        session.known = lib.known_ids_fast()
        stats = session.crawl_artists(sorted(session.artist_ids), max_works=max_works,
                                      dry_run=dry_run, order=order, s_mode=s_mode)
        if not dry_run:
            log("\n[i] 整理分类目录与索引…")
            info = session.finalize(rebuild=True)
            log(f"\n新下载图片 {info.get('downloaded', 0)} 张，"
                f"图库共 {info.get('total_records', 0)} 条记录")
            stats.update({"added": info.get("added", 0), "updated": info.get("updated", 0),
                          "total_records": info.get("total_records", 0)})
            finalize_run_record(session, lib, mode="artist",
                                artists=sorted(session.artist_ids),
                                params={"order": order, "s_mode": s_mode,
                                        "max_works": max_works},
                                result=stats, started=started, log=log)
        return stats
    if scope == "both" and not session.artist_ids:
        log("[!] 检索范围选了「两者都要」，但没有填画师 ID；本次按纯关键词处理。")
    state = SessionState(lib.state_dir / "crawl_state.json")
    if restart:
        state.reset()
        log("[i] 已重置爬取进度（--restart），所有组合都会重新翻页")
    session.state = state
    if state.exhausted:
        log(f"[i] 进度记忆：有 {len(state.exhausted)} 个「关键词+组合」上次已翻到接口尽头，本次自动跳过"
            "（要重爬请加 --restart）")
    if state.incomplete_run:
        log("[i] 检测到上次运行未走完，本次会接着爬未完成的组合（已下载的图会自动跳过）")
    session.known = lib.known_ids_fast()
    if session.known:
        log(f"[i] 图库已有 {len(session.known)} 个作品，重复的会自动跳过（--force 可强制重下）")
    if cfg.get("cookie_phpsessid") or cfg.get("refresh_token"):
        log("[i] 检测到登录凭据，将以登录状态访问"
            if session.client.logged_in else "[i] 已配置 Cookie（PHPSESSID）")
    else:
        log("[i] 未配置登录凭据 —— 匿名可搜索、可下原图，但可翻页数与内容较少"
            "（在 config.json 里填 cookie_phpsessid 可增强）")
    log(f"[i] 待处理关键词：{'、'.join(keywords)}（每种组合 {pages} 页，排序 {order}"
        + ("，深度模式" if deep else "") + ("，干跑侦察" if dry_run else "") + "）")
    _r18 = not parse_bool(cfg.get("skip_r18"), True)
    _r18g = _r18 and not parse_bool(cfg.get("skip_r18g"), True)
    log(f"[i] 内容分级：R-18 {'收' if _r18 else '不收'}，R-18G（猎奇）{'收' if _r18g else '不收'}，"
        f"动图 ugoira {'收' if not parse_bool(cfg.get('skip_ugoira'), True) else '不收'}")
    _range = parse_range(cfg)
    _mb = as_int(cfg.get("min_bookmarks"), 0)
    _ml = as_int(cfg.get("min_likes"), 0)
    if _range.active or _mb > 0 or _ml > 0:
        conds = []
        if _range.active:
            conds.append(f"发布时间 {_range.describe()}")
        if _mb > 0:
            conds.append(f"收藏 ≥ {_mb}")
        if _ml > 0:
            conds.append(f"点赞 ≥ {_ml}")
        log(f"[i] 筛选条件：{'；'.join(conds)}")
        if _mb > 0 or _ml > 0:
            log("[i] 提示：点赞/收藏数只能从作品详情取得，因此不达标作品的详情请求仍会发出（这是接口限制，不是重复下载）")

    started = time.time()
    info: Dict[str, Any]
    total_candidates = 0
    interrupted = False
    for kw in keywords:
        before = (session.downloaded, session.skipped, session.failed)
        try:
            stats = session.crawl_keyword(kw, pages, order, mode, s_mode, max_works,
                                          deep=deep, dry_run=dry_run)
        except KeyboardInterrupt:
            log("\n[!] 被用户中断，正在保存已下载内容与进度…")
            interrupted = True
            break
        except CrawlError as exc:
            log(f"[warn] 关键词「{kw}」失败：{exc}")
            continue
        total_candidates += int(stats.get("candidates") or 0)
        d, s, f = session.downloaded - before[0], session.skipped - before[1], session.failed - before[2]
        if not dry_run:
            lib.log_query(kw, d, s, f, pages)

    # 记录未走完的组合，便于下次续爬；全部走完则清空进度标记
    try:
        if not dry_run:
            remaining = [SessionState.make_key(kw, o, sm)
                         for kw in keywords
                         for _lbl, o, sm in session._combos(order, s_mode, deep)
                         if SessionState.make_key(kw, o, sm) not in state.exhausted]
            state.save(remaining, run_done=not remaining and not interrupted)
            if remaining and not interrupted:
                log(f"[i] 还有 {len(remaining)} 个组合本轮未走完（多半是达到 --limit 上限），"
                    "已记入进度文件，下次运行会继续")
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] 保存爬取进度失败（不影响已下载内容）：{exc}")

    if dry_run:
        log("\n========== 干跑结果（未下载任何文件）==========")
        log(f"这些关键词合计可下载 {total_candidates} 个作品。")
        log("确认无误后去掉 --dry-run 再跑一次即可真正下载。")
        return {"downloaded": 0, "skipped": 0, "failed": 0, "added": 0, "updated": 0,
                "total_records": len(lib.load_records()), "candidates": total_candidates}

    log("\n[i] 整理分类目录与索引…")
    try:
        info = session.finalize(rebuild=True)
    except KeyboardInterrupt:
        log("[!] 索引整理被中断，尝试仅写入记录…")
        added, updated = session._flush_index()
        info = {"added": added, "updated": updated, "downloaded": session.downloaded,
                "skipped": session.skipped, "failed": session.failed, "total_records": len(lib.load_records())}
    elapsed = time.time() - started
    finalize_run_record(session, lib, mode="keyword", keywords=keywords,
                        params={"pages": pages, "order": order, "mode": mode,
                                "s_mode": s_mode, "deep": deep, "max_works": max_works},
                        result=info, started=started, log=log)
    log("\n========== 本次结果 ==========")
    log(f"新下载图片：{info['downloaded']} 张，跳过（已存在）：{info['skipped']}，失败：{info['failed']}")
    sk_r18 = info.get("skipped_r18", 0)
    sk_r18g = info.get("skipped_r18g", 0)
    sk_ugo = info.get("skipped_ugoira", 0)
    if sk_r18 or sk_r18g or sk_ugo:
        parts = []
        if sk_r18:
            parts.append(f"R-18 {sk_r18} 个")
        if sk_r18g:
            parts.append(f"R-18G {sk_r18g} 个")
        if sk_ugo:
            parts.append(f"动图 {sk_ugo} 个")
        log(f"按设置跳过：{'、'.join(parts)}"
            + "（R-18 用 --keep-r18 放开；R-18G 需 --keep-r18g；动图用 --keep-ugoira）")
    fs = getattr(session, "filter_stats", {}) or {}
    if fs.get("date") or fs.get("like") or fs.get("skipped_ai") or fs.get("skipped_artist"):
        parts = []
        if fs.get("date"):
            parts.append(f"时间范围外 {fs['date']} 个")
        if fs.get("like"):
            parts.append(f"人气不达标 {fs['like']} 个")
        if fs.get("skipped_ai"):
            parts.append(f"AIGC 不符 {fs['skipped_ai']} 个")
        if fs.get("skipped_artist"):
            parts.append(f"画师不符 {fs['skipped_artist']} 个")
        log(f"按筛选条件排除：{'、'.join(parts)}")
    log(f"索引写入：新增 {info.get('added', 0)} 条，更新 {info.get('updated', 0)} 条，"
        f"图库共 {info.get('total_records', 0)} 条")
    log(f"分类链接：标签 {info.get('tag_links', 0)}，画师 {info.get('author_links', 0)}，"
        f"关键词 {info.get('keyword_links', 0)}"
        + (f"（其中复制 {info.get('copies', 0)} 个）" if info.get("copies") else "（均为硬链接，不额外占用空间）"))
    st = session.http.stats
    log(f"网络请求 {st['requests']} 次，传输 {format_size(st['bytes'])}，"
        f"重试 {st['retries']} 次"
        + (f"，限流 {st['rate_limited']} 次（累计等待 {st['total_wait']:.0f}s，"
           f"当前限速 {session.http.min_interval:.1f}s/请求）" if st.get("rate_limited") else "")
        + f"，耗时 {elapsed:.1f}s")
    log(f"图库目录：{lib.root}")
    log(f"索引文件：{lib.md_path} / {lib.csv_path} / {lib.sqlite_path}")
    return info


def cmd_search(args: argparse.Namespace, program_dir: Path) -> int:
    cfg, lib = prepare_cfg(args, program_dir)
    recs = lib.load_records()
    if not recs:
        out(f"图库还是空的（{lib.root}）。先运行：python pixiv_crawler.py crawl 你的关键词")
        return 1
    if args.interactive and not (args.words or args.tag or args.author or args.query or args.id
                                 or getattr(args, "artist_ids", None)):
        out("请输入检索词（标题/画师/标签/作品ID 都可以；多个词用空格分隔，直接回车则列出全部）：")
        try:
            line = input("检索> ").strip()
        except (EOFError, KeyboardInterrupt):
            line = ""
        if line:
            args.words = [w for w in re.split(r"[\s,，]+", line) if w]
    if args.list_tags:
        counter: Dict[str, int] = {}
        for r in recs:
            for t in r.get("tags") or []:
                counter[str(t)] = counter.get(str(t), 0) + 1
        out(f"共 {len(counter)} 个标签，按图数排序（前 {args.list_tags}）：")
        for i, (t, c) in enumerate(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[: args.list_tags], 1):
            out(f"{i:>4}. {c:>5}  {t}")
        return 0
    # 指定了作品 ID 时走精确匹配（--id 是精确语义），否则用完整筛选器
    if args.id:
        hits = search_records(
            lib, recs, text=" ".join(args.words or []), tags=args.tag or [],
            author=args.author or "", query=args.query or "", limit=args.limit, ids=args.id,
        )
    else:
        hits = search_records_advanced(
            recs,
            text=" ".join(args.words or []),
            tags=args.tag or [],
            author=args.author or "",
            author_ids=getattr(args, "artist_ids", None) or [],
            query=args.query or "",
            date_from=str(getattr(args, "date_from", None) or cfg.get("date_from") or ""),
            date_to=str(getattr(args, "date_to", None) or cfg.get("date_to") or ""),
            min_likes=as_int(getattr(args, "min_likes", None) or cfg.get("min_likes"), 0),
            min_bookmarks=as_int(
                getattr(args, "min_bookmarks", None) or cfg.get("min_bookmarks"), 0),
            r18_mode=str(getattr(args, "r18_mode", None) or "hide"),
            ai_mode=str(getattr(args, "ai_mode", None) or cfg.get("ai_mode") or "all"),
            limit=args.limit,
        )
    out(f"命中 {len(hits)} 条（图库共 {len(recs)} 条）" + (f"，最多显示 {args.limit} 条" if args.limit and len(hits) >= args.limit else ""))
    out("")
    print_records(hits, show_path=args.full_path)
    if args.export:
        p = Path(args.export)
        with open(native_path(p), "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "page", "title", "author", "tags", "file", "page_url"])
            for r in hits:
                w.writerow([r.get("id"), r.get("page"), r.get("title"), r.get("author"),
                            "|".join(str(t) for t in (r.get("tags") or [])), r.get("file"), r.get("page_url")])
        out(f"已导出检索结果：{p}")
    if args.open_first and hits:
        target = os.path.abspath(str(hits[0].get("file")))
        out(f"打开：{target}")
        try:
            os.startfile(target)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            out(f"打开失败：{exc}")
    return 0


def cookie_from_browser(verbose: bool = False) -> Optional[str]:
    """尝试从本机浏览器的 Cookie 库里读出 pixiv 的 PHPSESSID。

    复用同目录下 get_pixiv_login.py 里那份**已用官方向量验证过**的
    AES-256-GCM + Windows DPAPI 实现（零第三方依赖）。

    现实情况（已在 Edge 154 上实测）：Edge 127+/Chrome 127+ 会对 cookie 启用
    v20「应用绑定加密」，密钥受 SYSTEM 级 DPAPI 保护，普通权限读不出来；
    此时本函数返回 None，调用方应引导用户改为手动粘贴。
    另外浏览器运行期间 Cookie 库被独占，需要先完全退出浏览器。
    """
    import importlib.util

    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    helper = next((c / "browser_cookie.py" for c in (base, base / "_internal")
                   if (c / "browser_cookie.py").is_file()), None)
    if helper is None:
        if verbose:
            out("  [i] 未找到 browser_cookie.py（打包后可能缺失数据文件），跳过自动读取")
        return None
    try:
        spec = importlib.util.spec_from_file_location("pixiv_login_helper", helper)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            out(f"  [i] 载入 {helper.name} 失败：{exc}")
        return None

    profiles = mod.find_chromium_profiles() + mod.find_firefox_profiles()
    if not profiles:
        if verbose:
            out("  [i] 没有找到任何浏览器的 Cookie 库")
        return None
    for browser, db in profiles:
        profile = db.parent.parent.name
        try:
            entries = mod.read_entries(browser, db)
        except Exception as exc:  # noqa: BLE001
            if verbose:
                out(f"  [跳过] {browser}/{profile}：{exc}")
            continue
        for e in entries:
            if e.get("name", "").upper() == "PHPSESSID" and e.get("value") and not e.get("error"):
                if verbose:
                    out(f"  [找到] {browser}/{profile} 里的 PHPSESSID")
                return str(e["value"])
        if verbose:
            reasons = sorted({e.get("error") for e in entries if e.get("error")})
            if reasons:
                out(f"  [未取到] {browser}/{profile}：{reasons[0]}")
    return None


def cmd_auth(args: argparse.Namespace, program_dir: Path) -> int:
    """登录/登出/查看登录状态。上线给别人用时，这是唯一的登录入口。"""
    action = (args.action or "status").lower()
    cfg, _cfg_path = load_config(program_dir, getattr(args, "config", None))
    resolve_credentials(cfg, args)

    if action == "logout":
        removed = clear_credential_store()
        out(f"已删除凭据库：{CRED_FILE}" if removed else f"凭据库本来就不存在：{CRED_FILE}")
        out("注意：config.json 里的 cookie_phpsessid / refresh_token 不会被自动清除，"
            "如需彻底退出请手动清空这两个字段。")
        return 0

    if action == "doctor":
        out("=" * 68)
        out("读取能力体检：当前配置到底能读取多少内容")
        out("=" * 68)
        if args.offline:
            out("（--offline：只检查凭据配置，不访问 pixiv）")
            store = load_credential_store()
            out(f"凭据来源：{credentials_source(cfg, args)}")
            out(f"凭据库  ：{CRED_FILE}　{'存在' if CRED_FILE.is_file() else '不存在'}")
            out(f"  cookie_phpsessid：{'已配置 ' + str(len(str(cfg.get('cookie_phpsessid') or ''))) + ' 字符' if cfg.get('cookie_phpsessid') else '未配置'}")
            out(f"  refresh_token   ：{'已配置' if cfg.get('refresh_token') else '未配置'}")
            out("去掉 --offline 就会实际发请求探测可读取量。")
            return 0

        def say(msg: str) -> None:
            out(f"  {msg}")

        try:
            res = probe_capability(cfg, max_pages=int(getattr(args, "probe_pages", 0) or 24),
                                   skip_anon=bool(getattr(args, "skip_anon", False)),
                                   log=say)
        except KeyboardInterrupt:
            out("\n[!] 已中断")
            return 1
        out("")
        for name, state, desc in res["checks"]:
            tag = {"ok": "[OK]  ", "warn": "[WARN]", "bad": "[FAIL]"}.get(state, "[?]   ")
            out(f"  {tag} {name}：{desc}")
        cap = res.get("capacity") or {}
        if cap:
            out("")
            out(f"  可读取量明细：登录 {cap.get('login')} / 匿名 {cap.get('anon')}"
                f"（{cap.get('ratio')} 倍）")
            out(f"  pixiv 报告总数：登录 {cap.get('total_login')} / 匿名 {cap.get('total_anon')}")
        verdicts = {"full": "完全可用（登录权限已生效，可全量爬取）",
                    "limited": "受限（能爬，但有项目没通过，见下面的建议）",
                    "anonymous": "仅匿名（只能读到约 600 个/排序）",
                    "error": f"探测失败：{res.get('error') or '未知错误'}"}
        out("")
        out(f"结论：{verdicts.get(res['verdict'], res['verdict'])}")
        if res["advice"]:
            out("")
            out("下一步建议：")
            for i, a in enumerate(res["advice"], 1):
                out(f"  {i}. {a}")
            if any("加大探测深度" in a for a in res["advice"]):
                out(f"     → 命令行用法：auth doctor --probe-pages "
                    f"{max(20, int(getattr(args, 'probe_pages', 0) or 24) * 3)}")
                out("     → 图形界面：把「读取能力」页的「深度」调大后点「开始检测」")
            if any("跳过匿名对比" in a for a in res["advice"]):
                out("     → 命令行用法：去掉 --skip-anon（默认就会做匿名对比）")
                out("     → 图形界面：取消勾选「跳过匿名对比（更快）」")
        if res["verdict"] != "full":
            out("")
            out("获取/修复登录：")
            out("  python pixiv_crawler.py auth login            # 交互式（推荐）")
            out("  python pixiv_crawler.py auth login --method auto-cookie   # 从浏览器读 cookie")
            out("  图形界面：双击 gui.bat → 「读取能力」页，有按钮和图文指引")
        return 0 if res["verdict"] == "full" else 1

    if action == "status":
        out("=" * 68)
        out("pixiv 登录状态")
        out("=" * 68)
        out(f"凭据库文件：{CRED_FILE}")
        out(f"  存在：{'是' if CRED_FILE.is_file() else '否'}"
            + (f"（{format_size(file_size(CRED_FILE))}，更新于 "
               f"{load_credential_store().get('updated_at', '未知')}）" if CRED_FILE.is_file() else ""))
        if not cfg.get("refresh_token") and not cfg.get("cookie_phpsessid"):
            out("\n当前没有可用凭据 —— 处于匿名模式（可搜索、可下原图，但每种排序只能拿到约 600 个作品）。")
            out("登录方法：python pixiv_crawler.py auth login")
            return 0
        out(f"凭据来源：{credentials_source(cfg, args)}")
        out(f"凭据类型：{'refresh_token（App API）' if cfg.get('refresh_token') else 'PHPSESSID（网页）'}")
        if not args.offline:
            out("")
            client = PixivClient(HttpClient(cfg, verbose=False), cfg, verbose=False)
            info = client.show_login_status()
            if info and client.rotated_token:
                out("  [i] refresh_token 已轮换并自动存回凭据库")
        return 0

    # action == "login"
    kind = args.method or "auto"
    if kind not in ("auto", "token", "cookie", "auto-cookie"):
        out(f"[错误] 未知的登录方式：{kind}")
        return 2

    if kind in ("auto", "token") and sys.stdin.isatty():
        out("=" * 68)
        out("方式一（推荐）：refresh_token —— 走 pixiv 官方 OAuth，程序用 App API，")
        out("                可获取的作品最多，且凭据可长期使用。")
        out("方式二：自动读取 —— 从本机浏览器的 Cookie 库里取 PHPSESSID")
        out("                （需要先完全退出浏览器；新版 Edge/Chrome 可能读不出来）")
        out("方式三：手动粘贴 —— 自己从 DevTools 复制 PHPSESSID")
        out("=" * 68)
        try:
            choice = input("请选择 [1=refresh_token / 2=自动读取 / 3=手动粘贴]（回车默认 1）: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        kind = {"2": "auto-cookie", "3": "cookie"}.get(choice, "token")

    if kind == "auto-cookie":
        out("\n正在从本机浏览器读取 pixiv 登录态（只读本机文件，不上传任何数据）…")
        token = cookie_from_browser(verbose=True)
        if not token:
            out("\n没能自动读到 cookie。常见原因：")
            out("  · 浏览器还开着（Cookie 库被独占，请完全退出浏览器后重试）")
            out("  · 新版 Edge/Chrome（127+）启用了应用绑定加密，普通权限解不开")
            out("  · 浏览器里还没登录 pixiv")
            out("\n可以改用：python pixiv_crawler.py auth login --method cookie  （手动粘贴）")
            return 1
        token = token.strip()
        out(f"读到 PHPSESSID（{len(token)} 字符），正在向 pixiv 校验…")
        cfg_check: Dict[str, Any] = {"cookie_phpsessid": token, "refresh_token": ""}
        try:
            info = PixivClient(HttpClient(cfg_check, verbose=False), cfg_check,
                               verbose=False).verify_login()
        except CrawlError as exc:
            out(f"[错误] 校验时网络异常：{exc}")
            return 1
        if not info:
            out("[错误] 读到的 PHPSESSID 无法登录（可能已过期），请在浏览器里重新登录 pixiv。")
            return 1
        path = save_credential_store(token, kind="phpsessid",
                                     user={"id": info.get("user_id"), "name": info.get("name")})
        out(f"\n登录成功：{info.get('name')}（id={info.get('user_id')}）")
        out(f"凭据已保存到：{path}")
        return 0

    if kind == "cookie":
        return _auth_login_cookie(args)

    # ---- refresh_token：OAuth + PKCE ----
    verifier, challenge = pkce_pair()
    url = build_authorize_url(challenge)
    out("=" * 68)
    out("pixiv 登录（OAuth + PKCE）")
    out("=" * 68)
    out("第 1 步：在浏览器里打开下面这个地址（也可以直接点开）：")
    out("")
    out(f"  {url}")
    out("")
    out("第 2 步：用你的 pixiv 账号登录。如果浏览器里已登录过，会直接跳转。")
    out("第 3 步：登录后浏览器会跳到地址形如 https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?code=XXXX")
    out("        的页面（页面本身可能显示 404 或空白，这是正常的）。")
    out("第 4 步：把**浏览器地址栏里的完整地址**复制下来，粘贴到下面。")
    out("        （也可以只粘贴 code= 后面那串，程序都能识别）")
    out("")
    out("提示：账号密码只输入在 pixiv 页面上，本程序不接触你的密码。")
    out("-" * 68)
    try:
        if args.code:
            raw = args.code
            out(f"（已从命令行 --code 读取）")
        else:
            raw = input("请粘贴回调地址或 code: ").strip()
    except (EOFError, KeyboardInterrupt):
        out("\n已取消。")
        return 130
    code = extract_code_from_redirect(raw)
    if not code:
        out("[错误] 没能从你粘贴的内容里识别出 code。")
        out("        请确认复制的是形如 ...callback?code=xxxx 的完整地址。")
        return 2

    out("\n正在用授权码换取 token…")
    try:
        data = exchange_code_for_token(code, verifier)
    except CrawlError as exc:
        out(f"[错误] 换取 token 失败：{exc}")
        out("        常见原因：授权码已过期（请重新登录）、code 复制不完整、或网络需要代理。")
        return 1
    token = str(data.get("refresh_token") or "")
    if not token:
        out("[错误] pixiv 没有返回 refresh_token，请重试。")
        return 1
    user = data.get("user") or {}
    path = save_credential_store(token, kind="refresh_token",
                                 user={"id": str(user.get("id") or ""),
                                       "name": str(user.get("name") or ""),
                                       "account": str(user.get("account") or "")},
                                 extra={"scope": "app"})
    out("")
    out(f"登录成功：{user.get('name') or '(未返回昵称)'}"
        + (f"（@{user.get('account')}）" if user.get("account") else ""))
    out(f"凭据已保存到：{path}")
    out("（该文件只对你本人可读；请勿分享，也不要提交到 git）")
    out("")
    out("下一步：")
    out("  python pixiv_crawler.py auth status              # 确认登录状态")
    out("  python pixiv_crawler.py crawl 初音ミク --deep --pages 60")
    return 0


def exchange_code_for_token(code: str, verifier: str) -> Dict[str, Any]:
    """用 authorization_code + PKCE 换取 refresh_token / access_token。"""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    client_hash = hashlib.md5((ts + APP_HASH_SALT).encode("utf-8")).hexdigest()
    headers = {
        "User-Agent": APP_UA,
        "App-OS": "android",
        "App-OS-Version": "11",
        "App-Version": "5.0.234",
        "X-Client-Time": ts,
        "X-Client-Hash": client_hash,
        "X-Client-Id": APP_CLIENT_ID,
        "X-Client-Secret": APP_CLIENT_SECRET,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    body = urllib.parse.urlencode({
        "client_id": APP_CLIENT_ID,
        "client_secret": APP_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": OAUTH_REDIRECT,
        "include_policy": "true",
    }).encode("utf-8")
    req = urllib.request.Request(APP_TOKEN_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise CrawlError(f"HTTP {exc.code} {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise CrawlError(f"{type(exc).__name__}: {exc}") from exc


def _auth_login_cookie(args: argparse.Namespace) -> int:
    """PHPSESSID 登录：适合不想走 OAuth 的用户。"""
    out("=" * 68)
    out("用 PHPSESSID 登录")
    out("=" * 68)
    out("取法：")
    out("  1) 浏览器登录 https://www.pixiv.net/")
    out("  2) 按 F12 → Application（应用程序）→ 左侧 Storage → Cookies → https://www.pixiv.net")
    out("  3) 找到名为 PHPSESSID 的那一行，双击「值」列，全选复制")
    out("  4) 粘贴到下面（只粘贴这一串，不要带引号或其它 cookie）")
    out("")
    out("注意：部分新版浏览器（Edge/Chrome 127+）对 cookie 启用了应用绑定加密，")
    out("      你仍可从 DevTools 里**手工复制**该值，本程序只接收你粘贴的内容。")
    out("-" * 68)
    try:
        if args.cookie:
            raw = args.cookie
            out("（已从命令行 --cookie 读取）")
        else:
            raw = input("请粘贴 PHPSESSID: ").strip()
    except (EOFError, KeyboardInterrupt):
        out("\n已取消。")
        return 130
    token = raw.strip().strip('"').strip("'")
    if not token or len(token) < 20 or " " in token:
        out("[错误] 这看起来不像一个 cookie 值（应为较长的连续字符串，且不含空格）。")
        return 2
    out("\n正在向 pixiv 校验这个 cookie…")
    cfg: Dict[str, Any] = {"cookie_phpsessid": token, "refresh_token": ""}
    try:
        client = PixivClient(HttpClient(cfg, verbose=False), cfg, verbose=False)
        info = client.verify_login()
    except CrawlError as exc:
        out(f"[错误] 校验时网络异常：{exc}")
        return 1
    if not info:
        out("[错误] 这个 PHPSESSID 无法登录（可能抄错了、或已过期）。")
        out("        请重新在浏览器里登录 pixiv 后再复制一次。")
        return 1
    path = save_credential_store(token, kind="phpsessid",
                                 user={"id": info.get("user_id"), "name": info.get("name")})
    out("")
    out(f"登录成功：{info.get('name')}"
        + (f"（@{info.get('pixiv_id')}）" if info.get("pixiv_id") else "")
        + f" id={info.get('user_id')}")
    out(f"凭据已保存到：{path}")
    out("")
    out("下一步：python pixiv_crawler.py auth status")
    return 0


def cmd_selftest(args: argparse.Namespace, program_dir: Path) -> int:
    """自检：不下载任何图片，验证环境、配置、网络、登录凭据与图库索引。"""
    ok_count = [0]
    warn_count = [0]
    fail_count = [0]

    def ok(msg: str) -> None:
        ok_count[0] += 1
        out(f"  [OK]   {msg}")

    def warn(msg: str) -> None:
        warn_count[0] += 1
        out(f"  [WARN] {msg}")

    def fail(msg: str) -> None:
        fail_count[0] += 1
        out(f"  [FAIL] {msg}")

    out("=" * 68)
    out("pixiv 爬虫自检")
    out("=" * 68)

    # 1) 运行环境
    out("\n[1/7] 运行环境")
    out(f"  Python {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info >= (3, 9):
        ok("Python 版本满足要求（>=3.9）")
    else:
        fail(f"Python 版本过低：{sys.version.split()[0]}，请升级到 3.9 以上")
    try:
        import sqlite3 as _sq
        fts = any("FTS5" in str(r[0]) for r in _sq.connect(":memory:").execute("pragma compile_options"))
        ok(f"SQLite {_sq.sqlite_version}（FTS5 全文索引{'可用' if fts else '不可用，将只用 LIKE 检索'}）")
    except Exception as exc:  # noqa: BLE001
        warn(f"SQLite 检查失败：{exc}")
    try:
        import tkinter  # noqa: F401
        ok("tkinter 可用（graphical 界面 gui.bat 可用）")
    except Exception:  # noqa: BLE001
        warn("tkinter 不可用，只能用命令行模式（不影响爬取与检索）")
    try:
        root_free = shutil.disk_usage(native_path(program_dir)).free
        ok(f"程序所在磁盘剩余空间 {format_size(root_free)}")
    except OSError as exc:
        warn(f"磁盘空间检查失败：{exc}")

    # 2) 配置
    out("\n[2/7] 配置文件")
    cfg, lib = prepare_cfg(args, program_dir)
    cfg_path = find_config_path(program_dir, getattr(args, "config", None))
    if cfg_path and os.path.isfile(native_path(cfg_path)):
        ok(f"已加载配置：{cfg_path}")
    else:
        warn("未找到 config.json，正在使用内置默认值")
    try:
        ensure_dir(lib.root)
        probe = lib.root / ".write_probe"
        with open(native_path(probe), "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(native_path(probe))
        ok(f"图库目录可写：{lib.root}")
    except OSError as exc:
        fail(f"图库目录不可写：{lib.root}（{exc}）")
    if cfg.get("cookie_phpsessid"):
        ok(f"已配置 cookie_phpsessid（{len(str(cfg['cookie_phpsessid']))} 字符）")
    elif cfg.get("refresh_token"):
        ok("已配置 refresh_token")
    else:
        warn("未配置登录凭据 —— 匿名可搜索、可下原图，但可翻页数与内容较少")

    # 3) 登录凭据逻辑（离线验证，不发真实请求）
    out("\n[3/7] 登录凭据处理逻辑（离线验证，不访问 pixiv）")
    _selftest_credentials(cfg, ok, warn, fail)

    # 3.5) 登录态是否真的有效（这一步会访问 pixiv）
    out("\n[4/7] 登录态有效性（真实校验）")
    if getattr(args, "offline", False):
        warn("已指定 --offline，跳过登录态校验")
    else:
        try:
            probe_client = PixivClient(HttpClient(cfg, verbose=False), cfg, verbose=False)
            if not (cfg.get("cookie_phpsessid") or cfg.get("refresh_token")):
                warn("未配置登录凭据，当前为匿名模式（这是可以正常爬取的，只是可获取量较少）")
            elif probe_client.show_login_status():
                ok("登录态已通过 pixiv 服务器校验")
            else:
                fail("配置了登录凭据但校验未通过 —— 按上面的提示重新获取后再试")
        except CrawlError as exc:
            warn(f"校验过程中网络异常：{exc}")

    # 4) 网络
    out("\n[5/7] 网络连通性")
    if getattr(args, "offline", False):
        warn("已指定 --offline，跳过网络测试")
    else:
        probe_http = HttpClient(cfg, verbose=False)
        try:
            status, body = probe_http.request(
                ILLUST_URL.format(iid="1"), headers={}, allow_404=True
            )
            if status == 200:
                ok(f"可访问 pixiv 网页接口（HTTP {status}，{len(body)} 字节）")
            else:
                ok(f"可访问 pixiv 接口（HTTP {status}，作品 ID 1 不存在属正常）")
        except CrawlError as exc:
            fail(f"无法访问 pixiv：{exc}")
            out("         → 若需代理，请在 config.json 里填 proxy，或加 --proxy http://127.0.0.1:7890")
        try:
            iid = _pick_illust_id(lib, cfg, probe_http)
            if iid:
                status, _ct, body = _probe_image(probe_http, cfg, iid)
                if status == 200 and body > 0:
                    ok(f"可下载原图（样本 {iid} 的第 1 页，{format_size(body)}）")
                else:
                    warn(f"原图下载探测返回 HTTP {status}")
            else:
                warn("图库为空，跳过原图下载测试（跑一次 crawl 后会自动启用）")
        except CrawlError as exc:
            fail(f"原图下载失败：{exc}")

    # 5) 图库完整性
    out("\n[6/7] 图库索引完整性")
    recs = lib.load_records()
    if not recs:
        warn(f"图库为空（{lib.root}），先运行：python pixiv_crawler.py crawl 你的关键词")
    else:
        works = {str(r.get("id")) for r in recs}
        ok(f"索引 {len(recs)} 条记录，覆盖 {len(works)} 个作品")
        missing = [r for r in recs if not os.path.exists(native_path(lib.root / str(r.get("file"))))]
        if missing:
            fail(f"有 {len(missing)} 条记录指向的文件不存在，例如 {missing[0].get('file')}")
        else:
            ok("索引中的每个文件都真实存在")
        seen: Dict[Tuple[str, int], int] = {}
        for r in recs:
            k = (str(r.get("id")), int(r.get("page") or 0))
            seen[k] = seen.get(k, 0) + 1
        dup = [k for k, v in seen.items() if v > 1]
        if dup:
            warn(f"有 {len(dup)} 组重复记录（如作品 {dup[0][0]} p{dup[0][1]}），可运行 reindex 重整")
        else:
            ok("没有重复记录")
        for name, path in (("index.csv", lib.csv_path), ("index.md", lib.md_path),
                           ("catalog.sqlite", lib.sqlite_path)):
            if os.path.isfile(native_path(path)):
                ok(f"{name} 存在（{format_size(file_size(path))}）")
            else:
                warn(f"{name} 缺失，运行 reindex 可重建")
        probe = str(next(iter(works)))
        hit = search_records(lib, recs, ids=[probe], limit=1)
        if hit:
            ok(f"按作品 ID 检索可用（用 {probe} 试查命中）")
        else:
            fail(f"按作品 ID 检索失败（{probe} 查不到）")
        tag_probe = next((str(t) for r in recs for t in (r.get("tags") or [])), "")
        if tag_probe:
            if search_records(lib, recs, tags=[tag_probe], limit=1):
                ok(f"按标签检索可用（用「{tag_probe}」试查命中）")
            else:
                fail(f"按标签检索失败（「{tag_probe}」查不到）")

    # 6) 分类目录
    out("\n[7/7] 分类目录")
    if recs:
        for name, base in (("by_tag", lib.by_tag), ("by_author", lib.by_author),
                           ("by_keyword", lib.by_keyword)):
            if os.path.isdir(native_path(base)):
                n = sum(len(fs) for _r, _d, fs in os.walk(native_path(base)))
                ok(f"{name} 存在，共 {n} 个链接")
            elif parse_bool(cfg.get(f"make_{name[3:]}_links"), True):
                warn(f"{name} 不存在，运行 reindex --relink 可生成")
            else:
                ok(f"{name} 已按配置关闭")
        try:
            sample = next((lib.root / str(r["file"]) for r in recs
                           if os.path.exists(native_path(lib.root / str(r["file"])))), None)
            if sample is not None:
                st = os.stat(native_path(sample))
                if st.st_nlink > 1:
                    ok(f"分类目录使用硬链接（样本 nlink={st.st_nlink}，不额外占用空间）")
                else:
                    warn("样本文件 nlink=1，分类目录可能未建立（或已是复制模式）")
        except OSError:
            pass
    else:
        warn("图库为空，跳过分类目录检查")

    out("\n" + "=" * 68)
    out(f"自检结果：通过 {ok_count[0]} 项，警告 {warn_count[0]} 项，失败 {fail_count[0]} 项")
    if fail_count[0]:
        out("存在失败项，请按上面的提示处理后重试。")
        return 1
    if warn_count[0]:
        out("没有致命问题；警告项按需处理即可，可直接开始爬取。")
    else:
        out("一切正常，可以开始爬取。")
    out("=" * 68)
    return 0


def _selftest_credentials(cfg: Dict[str, Any], ok, warn, fail) -> None:
    """用本地模拟服务器验证 cookie / refresh_token 的处理逻辑（不接触真实 pixiv）。"""
    import http.server
    import threading as _threading

    cookie = str(cfg.get("cookie_phpsessid") or "").strip()
    if cookie:
        client = PixivClient(HttpClient(cfg, verbose=False), {**cfg, "refresh_token": ""}, verbose=False)
        headers = client._auth_headers()
        if headers.get("Cookie") == f"PHPSESSID={cookie}":
            ok("cookie 已正确组装为请求头 Cookie: PHPSESSID=***")
        else:
            fail("cookie 组装异常，未能生成 PHPSESSID 请求头")

    token = str(cfg.get("refresh_token") or "").strip()
    if not token:
        if not cookie:
            warn("未配置登录凭据，跳过该项")
        return

    captured: Dict[str, Any] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            captured["body"] = self.rfile.read(length).decode("utf-8", "replace")
            captured["ctype"] = self.headers.get("Content-Type") or ""
            payload = json.dumps({
                "access_token": "SELFTEST_TOKEN",
                "refresh_token": "SELFTEST_REFRESH",
                "user": {"id": "12345", "name": "selftest"},
                "expires_in": 3600,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_url = globals()["APP_TOKEN_URL"]
    try:
        globals()["APP_TOKEN_URL"] = f"http://127.0.0.1:{port}/auth/token"
        client = PixivClient(HttpClient(cfg, verbose=False), cfg, verbose=False)
        if client.access_token == "SELFTEST_TOKEN" and client.logged_in:
            ok("refresh_token 换 access_token 流程正确（用本地模拟服务器验证）")
        else:
            fail(f"refresh_token 换 token 失败（拿到 {client.access_token!r}）")
        body = captured.get("body") or ""
        if "grant_type=refresh_token" in body and "client_id=" in body:
            ok("token 请求体参数完整（grant_type / client_id / client_secret / refresh_token）")
        else:
            fail(f"token 请求体不完整：{body[:120]!r}")
        if "refresh_token" in body:
            ok("凭据已放入请求体（非 URL，避免泄漏到日志）")
        headers = client._auth_headers()
        if headers.get("Authorization") == "Bearer SELFTEST_TOKEN":
            ok("后续请求会带上 Authorization: Bearer ***")
        else:
            fail("Authorization 请求头组装异常")
    except Exception as exc:  # noqa: BLE001
        fail(f"登录流程自检异常：{type(exc).__name__}: {exc}")
    finally:
        globals()["APP_TOKEN_URL"] = real_url
        server.shutdown()
        server.server_close()


def _pick_illust_id(lib: Library, cfg: Dict[str, Any], http: HttpClient) -> str:
    """挑一个作品 ID 用于原图下载探测：优先用图库里已有的，否则取搜索结果里第一个。"""
    recs = lib.load_records()
    for r in recs:
        if str(r.get("url") or "").startswith("http"):
            return str(r.get("id") or "")
    try:
        client = PixivClient(http, cfg, verbose=False)
        items, _extra = client.search_illusts("初音ミク", 1, order="date", mode="all", s_mode="s_tag")
        for it in items:
            if int(it.get("xRestrict") or 0) == 0:
                return str(it.get("id") or "")
    except CrawlError:
        return ""
    return ""


def probe_url_size(http: HttpClient, cfg: Dict[str, Any], url: str) -> Tuple[int, str, int]:
    """探测某个图片 URL 能否下载（HEAD，失败则 Range 取 1 字节）。返回 (状态码, 类型, 字节数)。

    抽出来是为了让"读取能力体检"能直接探测已知的原图地址，不必再取一遍详情。
    """
    headers = {"User-Agent": str(cfg.get("user_agent") or DESKTOP_UA), "Referer": PIXIV_HOME}

    def _try(method: str, extra: Optional[Dict[str, str]] = None) -> Tuple[int, str, int]:
        h = dict(headers)
        if extra:
            h.update(extra)
        req = urllib.request.Request(url, headers=h, method=method)
        try:
            with http.opener.open(req, timeout=float(cfg.get("timeout") or 30)) as resp:
                if method == "HEAD":
                    return (int(resp.status), str(resp.headers.get("Content-Type") or ""),
                            int(resp.headers.get("Content-Length") or 0))
                chunk = resp.read(1)
                return int(resp.status), str(resp.headers.get("Content-Type") or ""), len(chunk)
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
            try:
                exc.close()
            except Exception:
                pass
            return code, "", 0

    status, ctype, size = _try("HEAD")
    if status == 200 and size > 0:
        return status, ctype, size
    status2, ctype2, size2 = _try("GET", {"Range": "bytes=0-0"})
    if status2 in (200, 206):
        return 200, ctype2 or ctype, size2 or size
    return status2 or status, ctype2 or ctype, size2 or size


def _probe_image(http: HttpClient, cfg: Dict[str, Any], illust_id: str) -> Tuple[int, str, int]:
    """只做元信息探测（HEAD，失败则取 1 字节），验证原图下载通道是否可用。"""
    client = PixivClient(http, cfg, verbose=False)
    detail = client.illust_detail(illust_id)
    if not detail:
        return 0, "", 0
    urls = client.resolve_original_urls(illust_id, detail)
    if not urls:
        return 0, "", 0
    return probe_url_size(http, cfg, urls[0]["url"])


def cmd_repair(args: argparse.Namespace, program_dir: Path) -> int:
    """按索引找出缺失的页并补齐（不依赖搜索结果，因此不会漏掉任何作品）。

    典型场景：早期用 --max-pages-per-work 只下了一页；或磁盘上文件被误删。
    加 --clean-tags 可同时清洗 pixiv 偶发返回的拼接标签（如 '初音ミク,'）。
    """
    cfg, lib = prepare_cfg(args, program_dir)
    recs = lib.load_records()
    if not recs:
        out(f"图库为空（{lib.root}），无需修复。")
        return 0

    # ---- 清洗拼接标签（--clean-tags）----
    if getattr(args, "clean_tags", False):
        fixed = 0
        for r in recs:
            tags = r.get("tags") or []
            cleaned = [x for t in tags for x in _split_tag(str(t))]
            if cleaned != [str(t).strip() for t in tags]:
                r["tags"] = cleaned
                fixed += 1
        if fixed:
            # 写回前剔除合并进来的 d_tags，保持 records.jsonl 干净（原数据不含衍生标签）
            for r in recs:
                r.pop("d_tags", None)
            lib.upsert_records(recs)
            out(f"[修复] 清洗了 {fixed} 条记录的拼接标签（含逗号/顿号的标签已拆开）。")
        else:
            out("[修复] 没有发现拼接标签，无需清洗。")
        return 0

    by_work: Dict[str, Dict[str, Any]] = {}
    for r in recs:
        iid = str(r.get("id"))
        e = by_work.setdefault(iid, {"pages": set(), "record": r, "title": r.get("title")})
        e["pages"].add(int(r.get("page") or 0))

    # 1) 索引里有记录、但磁盘文件不见了的
    missing_files: List[Dict[str, Any]] = []
    for r in recs:
        if not os.path.exists(native_path(lib.root / str(r.get("file")))):
            missing_files.append(r)
    # 2) 作品页数不完整的（记录里声明的页数 > 实际拥有的页）
    gaps: List[Tuple[str, int, int]] = []
    for iid, e in by_work.items():
        declared = int(e["record"].get("page_count") or len(e["pages"]) or 1)
        have = sorted(e["pages"])
        if len(have) < declared:
            gaps.append((iid, declared, len(have)))

    out(f"图库共 {len(recs)} 条记录 / {len(by_work)} 个作品")
    out(f"磁盘缺失文件：{len(missing_files)} 个")
    out(f"页数不完整的作品：{len(gaps)} 个")
    if not missing_files and not gaps:
        out("一切完整，无需修复。")
        return 0

    if args.dry_run:
        for r in missing_files[:20]:
            out(f"  [缺文件] {r.get('id')}_p{r.get('page')}  {r.get('file')}")
        for iid, declared, have in gaps[:20]:
            out(f"  [缺页] {iid}  声明 {declared} 页，实际 {have} 页 —— {by_work[iid]['title']}")
        if len(missing_files) + len(gaps) > 20:
            out(f"  …（其余 {len(missing_files) + len(gaps) - 20} 项略）")
        out("\n这是 --dry-run，未做任何修复。去掉 --dry-run 即可真正补齐。")
        return 0

    session = CrawlSession(cfg, lib, verbose=True)
    session.known = lib.known_ids_fast()
    # repair 的目的是把作品补齐，所以不能受「每个作品最多下几页」限制，
    # 否则被截断的那部分永远补不上（这正是缺页产生的原因之一）。
    if as_int(session.cfg.get("max_pages_per_work"), 0) > 0:
        out("[i] repair 会忽略 max_pages_per_work（否则缺的页永远补不齐）")
        session.cfg["max_pages_per_work"] = 0
    concurrency = max(1, int(cfg.get("concurrency") or 4))
    targets = sorted({iid for iid, _d, _h in gaps} | {str(r.get("id")) for r in missing_files})
    if args.limit and args.limit > 0:
        targets = targets[: args.limit]
    out(f"\n开始检查并补齐 {len(targets)} 个作品…\n")

    fixed_works = 0
    pending: List[Dict[str, Any]] = []
    for n, iid in enumerate(targets, 1):
        rec = by_work.get(iid, {}).get("record") or {}
        brief = {
            "id": iid,
            "title": rec.get("title") or "",
            "pageCount": 0,
            "xRestrict": 0,
            "illustType": 2 if str(rec.get("type") or "") == "ugoira" else 0,
        }
        try:
            detail, tasks = session.prepare_work(
                brief, str(rec.get("query") or "repair"), int(rec.get("rank") or 0),
                str(rec.get("order") or "date"),
                list(rec.get("query_keys") or [rec.get("query") or "repair"]),
            )
        except CrawlError as exc:
            out(f"  [warn] {iid} 处理失败：{exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            out(f"  [warn] {iid} 异常：{type(exc).__name__}: {exc}")
            continue
        if detail is None:
            continue
        need = [t for t in tasks if t["need"]]
        if not need:
            continue
        got = int(rec.get("page_count") or 0)
        out(f"  [{n}/{len(targets)}] 补齐 {iid}：「{rec.get('title')}」 需下载 {len(need)} 页"
            f"（原记录 {got} 页 / 实际共 {len(tasks)} 页）")
        pending.extend(need)
        fixed_works += 1
        if len(pending) >= 60:
            session._drain(pending, concurrency)
            pending = []
            session._flush_index()
        time.sleep(float(cfg.get("search_delay") or 0.3))
    if pending:
        session._drain(pending, concurrency)
    session._flush_index()

    out("\n[i] 重新整理索引…")
    info = session.finalize(rebuild=True)
    out(f"\n修复完成：涉及 {fixed_works} 个作品，新下载 {session.downloaded} 张，失败 {session.failed}")
    out(f"图库共 {info.get('total_records', 0)} 条记录（新增 {info.get('added', 0)}）")
    return 0


def run_follow_sync(cfg: Dict[str, Any], lib: Library, store, targets: Sequence[Any], *,
                    dry_run: bool = False, order: str = "date",
                    stop_event: Optional[Any] = None, progress_cb: Optional[Any] = None,
                    log=out, started: float = 0.0) -> Dict[str, int]:
    """追更同步主流程（CLI 与图形界面共用）。

    对清单里每位画师跑 crawl_artist（增量：已下载的自动跳过），记录上次追更时间，
    结束时统一 finalize + 写爬取历史。返回 {new, downloaded, failed, records}。
    """
    session = CrawlSession(cfg, lib, verbose=True)
    if stop_event is not None:
        session.stop_event = stop_event
    if progress_cb is not None:
        session.progress_cb = progress_cb
    session.known = lib.known_ids_fast()
    session.dry_run = dry_run
    pages = int(cfg.get("pages") or 1)
    max_works = int(cfg.get("max_works_per_run") or 0)
    started = started or time.time()
    log("=" * 68)
    log(f"追更 {len(targets)} 位画师" + ("（干跑，不下载）" if dry_run else "")
        + f"　图库现有 {len(session.known)} 个作品")
    log("=" * 68)

    total_new = total_down = total_fail = 0
    for a_idx, a in enumerate(targets, 1):
        if session.should_stop():
            log("[停止] 用户请求停止，跳过剩余画师")
            break
        session.report(stage="follow", done=a_idx, total=len(targets), detail=a.label())
        try:
            stats = session.crawl_artist(a.artist_id, name=a.name, max_works=max_works,
                                         pages=pages, dry_run=dry_run, order=order)
        except KeyboardInterrupt:
            log("\n[!] 被中断，已下载的内容与索引均已保存。")
            break
        except CrawlError as exc:
            log(f"[warn] 画师 {a.label()} 追更失败：{exc}")
            continue
        total_new += int(stats.get("new") or 0)
        total_down += int(stats.get("downloaded") or 0)
        total_fail += int(stats.get("failed") or 0)
        if not dry_run:
            store.mark_synced(a.artist_id)
            store.save()
            lib.log_query(f"artist:{a.artist_id}", int(stats.get("downloaded") or 0), 0,
                          int(stats.get("failed") or 0), 0)

    if not dry_run:
        log("\n[i] 整理分类目录与索引…")
        info = session.finalize(rebuild=True)
        session.dry_run = False
        finalize_run_record(session, lib, mode="follow",
                            artists=[a.artist_id for a in targets],
                            params={"pages": pages, "order": order},
                            result=info, started=started, log=log)
        log(f"\n========== 追更结果 ==========")
        log(f"新增作品 {total_new} 个，新下载 {total_down} 张，失败 {total_fail}")
        log(f"图库共 {info.get('total_records', 0)} 条记录"
            f"（标签链接 {info.get('tag_links', 0)}，画师链接 {info.get('author_links', 0)}）")
    else:
        log(f"\n[dry-run] 共发现新增作品 {total_new} 个，未下载任何文件。")
        log("去掉 --dry-run 即可真正下载。")
    log(f"耗时 {time.time() - started:.1f}s")
    return {"new": total_new, "downloaded": total_down, "failed": total_fail,
            "records": len(lib.load_records())}


def cmd_follow(args: argparse.Namespace, program_dir: Path) -> int:
    """画师追更：订阅清单管理 + 增量下载新作品。

    这是长期使用的主力模式——关键词搜索是"一次性"的，追更是"订阅制"的：
    反复运行只会下载新出的作品，已下载的自动跳过。
    """
    if artists_mod is None:
        out("[错误] 缺少 artists.py，无法使用追更功能。")
        return 1
    cfg, lib = prepare_cfg(args, program_dir)
    store = artists_mod.ArtistStore(lib.root / artists_mod.ARTISTS_FILENAME)
    action = (args.action or "list").lower()

    # ---- 订阅清单管理 ----
    if action == "list":
        out(f"追更清单：{store.path}")
        if not store.artists:
            out("（空）用下面的命令添加画师：")
            out("    python pixiv_crawler.py follow add https://www.pixiv.net/users/73260619")
            out("    python pixiv_crawler.py follow add 73260619 四宮いずな")
            return 0
        out(f"共 {len(store.artists)} 位画师：\n")
        for i, a in enumerate(store.artists, 1):
            line = f"{i:>3}. {a.label()}"
            if a.last_sync:
                line += f"   上次追更 {a.last_sync[:19].replace('T', ' ')}"
            else:
                line += "   尚未追更"
            if a.note:
                line += f"   # {a.note}"
            out(line)
        out(f"\n下一步：python pixiv_crawler.py follow sync"
            + ("  （先加 --dry-run 看看会下多少）" if not args.dry_run else ""))
        return 0

    if action == "add":
        if not args.targets:
            out("用法：follow add <画师ID或主页链接> [画师名]")
            out("  例：follow add https://www.pixiv.net/users/73260619 四宮いずな")
            out("      follow add 73260619 --name 四宮いずな     （一次加多位时请用 --name）")
            return 2
        added = 0
        last: Optional[Any] = None
        for raw in args.targets:
            aid = artists_mod.parse_artist_ref(raw)
            if not aid:
                # 容错：紧跟在 ID 后面、看起来像名字的片段，当作上一位画师的名字
                if last is not None and not args.name:
                    last.name = raw.strip()
                    out(f"  [提示] 把 {raw!r} 记为 {last.label()} 的名字"
                        "（一次加多位画师请用 --name）")
                    continue
                out(f"  [跳过] 看不懂这个输入：{raw!r}"
                    "（支持 https://www.pixiv.net/users/12345 或纯数字 ID）")
                continue
            a, is_new = store.add(aid, name=args.name or "", note=args.note or "")
            added += 1 if is_new else 0
            out(f"  {'已添加' if is_new else '已存在'}：{a.label()}")
            last = a
        if added or last is not None:
            store.save()
            out(f"\n清单已更新：{store.path}（共 {len(store.artists)} 位）")
        return 0

    if action == "remove":
        if not args.targets:
            out("用法：follow remove <画师ID或主页链接>")
            return 2
        removed = 0
        for raw in args.targets:
            aid = artists_mod.parse_artist_ref(raw) or raw.strip()
            if store.remove(aid):
                removed += 1
                out(f"  已移除：{aid}")
            else:
                out(f"  清单里没有：{aid}")
        if removed:
            store.save()
        return 0

    if action != "sync":
        out(f"[错误] 未知的操作：{action}（可用 add / remove / list / sync）")
        return 2

    # ---- 追更 ----
    if not store.artists:
        out(f"追更清单是空的（{store.path}）。先加画师：")
        out("    python pixiv_crawler.py follow add https://www.pixiv.net/users/73260619")
        return 1
    targets = store.artists
    if args.targets:
        wanted = {artists_mod.parse_artist_ref(t) or t.strip() for t in args.targets}
        targets = [a for a in store.artists if a.artist_id in wanted]
        if not targets:
            out("指定的画师不在清单里。先用 follow list 看看有哪些。")
            return 1

    session = CrawlSession(cfg, lib, verbose=True)
    return run_follow_sync(cfg, lib, store, targets, dry_run=bool(args.dry_run),
                           order=str(getattr(args, "order", None) or "date"), log=out)["new"]


def cmd_stats(args: argparse.Namespace, program_dir: Path) -> int:
    cfg, lib = prepare_cfg(args, program_dir)
    recs = lib.load_records()
    out(f"图库目录：{lib.root}")
    if not recs:
        out("（空）")
        return 0
    works = {str(r.get("id")) for r in recs}
    authors = {str(r.get("author")) for r in recs}
    tags: Dict[str, int] = {}
    kws: Dict[str, int] = {}
    for r in recs:
        for t in r.get("tags") or []:
            tags[str(t)] = tags.get(str(t), 0) + 1
        for k in r.get("query_keys") or ([r.get("query")] if r.get("query") else []):
            kws[str(k)] = kws.get(str(k), 0) + 1
    total = sum(int(r.get("bytes") or 0) for r in recs)
    out(f"作品数：{len(works)}　图片数：{len(recs)}　画师数：{len(authors)}　标签数：{len(tags)}")
    out(f"原图占用：{format_size(total)}")
    disk = shutil.disk_usage(native_path(lib.root))
    out(f"所在磁盘剩余：{format_size(disk.free)}")
    out("\nTop 15 标签：")
    for t, c in sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
        out(f"  {c:>5}  {t}")
    if kws:
        out("\n按关键词：")
        for k, c in sorted(kws.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
            out(f"  {c:>5}  {k}")
    qpath = lib.queries_path
    if os.path.isfile(native_path(qpath)):
        out(f"\n爬取历史：{qpath}")
    return 0


def cmd_reindex(args: argparse.Namespace, program_dir: Path) -> int:
    cfg, lib = prepare_cfg(args, program_dir)
    info = lib.rebuild_all()
    out(f"已重建索引：{info['records']} 条")
    out(f"  {info['csv']}")
    out(f"  {info['md']}")
    out(f"  {info['sqlite']}")
    if args.relink:
        recs = lib.load_records()
        cfg2 = dict(cfg)
        cfg2["make_tag_links"] = True
        cfg2["make_author_links"] = True
        cfg2["make_keyword_links"] = True
        session = CrawlSession(cfg2, lib, verbose=False)
        stale = session.cleanup_stale_links(recs)
        if stale:
            out(f"清理失效链接：{stale} 个")
        session.new_records = recs
        stats = session.classify(recs)
        out(f"已补齐分类链接：标签 {stats.get('tag_links', 0)}，画师 {stats.get('author_links', 0)}，"
            f"关键词 {stats.get('keyword_links', 0)}")
    return 0


def cmd_gui(args: argparse.Namespace, program_dir: Path) -> int:
    """零依赖图形界面（tkinter）。三个标签页：

      * 爬取     —— 关键词、页数、排序、分级、时间范围、人气门槛
      * 图库检索 —— 按标签(可多选)/画师/点赞/收藏/发布时间/R-18 组合筛选已下载的图库
      * 图库位置 —— 查看与更改图库在电脑中的位置，重建索引

    说明：tkinter 控件只能在主线程操作，所以后台爬取线程把日志放进 queue，
    由主线程定时取出渲染；否则会出现随机崩溃或界面卡死。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        out("当前 Python 未包含 tkinter，请改用命令行模式：python pixiv_crawler.py crawl 关键词")
        return 1

    import queue

    DONE = "__CRAWL_DONE__"
    ANY = "不限"
    cfg, lib = prepare_cfg(args, program_dir)
    cfg_path = find_config_path(program_dir, getattr(args, "config", None)) or (program_dir / "config.json")
    L = {"cfg": cfg, "lib": lib, "path": cfg_path, "recs": [], "busy": False}

    root = tk.Tk()
    root.title(f"pixiv 爬虫 v{__version__}")
    root.geometry("1180x760")

    msg_q: "queue.Queue[str]" = queue.Queue()
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)

    # ==================================================================================
    # 状态栏（常驻底部：显示图库位置与记录数）
    # ==================================================================================
    bar = ttk.Frame(root, padding=(8, 3))
    bar.pack(fill="x", side="bottom")
    lib_path_var = tk.StringVar(value=f"图库位置：{L['lib'].root}")
    lib_count_var = tk.StringVar(value="记录：—")
    ttk.Label(bar, textvariable=lib_path_var, foreground="#06c").pack(side="left")
    ttk.Label(bar, textvariable=lib_count_var, foreground="#666").pack(side="right")

    def open_path(p: "Path | str") -> None:
        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法打开", f"{p}\n{exc}")

    # ==================================================================================
    # 发布时间选择器：年 / 月 / 日 三级下拉（爬取页与图库检索页共用同一份实现）
    # ==================================================================================
    def build_date_range(parent: tk.Misc, init_from: Any, init_to: Any, *,
                         prefix: str = "发布时间：", hint_row: bool = True,
                         on_change: Any = None) -> Dict[str, Any]:
        """在 parent 里创建一组"起止时间"三级下拉，返回操作句柄。

        为什么做成工厂：爬取页和图库检索页都需要同一套交互（年月日联动、闰年适配、
        整年/整月语义、起止矛盾提示）。复制两份必然走样，所以只保留一份实现。

        返回 dict 含：
            from_value()/to_value()  取当前起止（配置用的日期串，空串=不限）
            set_values(f, t)         用日期串回填
            refresh_hint()           重新计算提示文字
            hint_var / error         提示文本变量 与 错误状态容器
        """
        row = ttk.Frame(parent)
        row.pack(fill="x")
        if prefix:
            ttk.Label(row, text=prefix).pack(side="left")
        ttk.Label(row, text="从").pack(side="left")

        def parse_cfg(text: Any) -> Tuple[Optional[int], Optional[int], Optional[int]]:
            dt = parse_user_date(text)
            return (dt.year, dt.month, dt.day) if dt else (None, None, None)

        fy, fm, fd = parse_cfg(init_from)
        ty, tm, td = parse_cfg(init_to)
        this_year = datetime.now().year
        years = [ANY] + [str(y) for y in range(2007, this_year + 2)]
        months = [ANY] + [str(m) for m in range(1, 13)]
        yf_var = tk.StringVar(value=str(fy) if fy else ANY)
        mf_var = tk.StringVar(value=str(fm) if fm else ANY)
        df_var = tk.StringVar(value=str(fd) if fd else ANY)
        yt_var = tk.StringVar(value=str(ty) if ty else ANY)
        mt_var = tk.StringVar(value=str(tm) if tm else ANY)
        dt_var = tk.StringVar(value=str(td) if td else ANY)

        def cbox(var: tk.StringVar, values: List[str], width: int) -> ttk.Combobox:
            # pack 直接写在工厂里：早期版本在调用处漏写 pack，
            # 结果下拉"创建了但从未布局"（几何 1x1），界面上是空白小框
            cb = ttk.Combobox(row, textvariable=var, values=values, width=width,
                              state="readonly")
            cb.pack(side="left")
            return cb

        def days_in(year: int, month: int) -> int:
            nxt = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
            return (nxt - timedelta(days=1)).day

        def value_from(y: str, m: str, d: str) -> str:
            if y == ANY:
                return ""
            if m == ANY:
                return y
            if d == ANY:
                return f"{y}-{int(m):02d}"
            return f"{y}-{int(m):02d}-{int(d):02d}"

        yf_cb = cbox(yf_var, years, 9)
        ttk.Label(row, text="年").pack(side="left")
        mf_cb = cbox(mf_var, months, 6)
        ttk.Label(row, text="月").pack(side="left")
        df_cb = cbox(df_var, [ANY], 6)
        ttk.Label(row, text="日").pack(side="left")
        ttk.Label(row, text="\u3000到").pack(side="left")
        yt_cb = cbox(yt_var, years, 9)
        ttk.Label(row, text="年").pack(side="left")
        mt_cb = cbox(mt_var, months, 6)
        ttk.Label(row, text="月").pack(side="left")
        dt_cb = cbox(dt_var, [ANY], 6)
        ttk.Label(row, text="日").pack(side="left")
        ttk.Label(row, text="（结束日含当天；月/日留「不限」代表整年或整月）",
                  foreground="#777").pack(side="left", padx=(6, 0))

        state: Dict[str, Any] = {"error": [False]}
        if hint_row:
            hr = ttk.Frame(parent, padding=(0, 0, 0, 4))
            hr.pack(fill="x")
            state["hint_var"] = tk.StringVar(value="")
            state["hint"] = ttk.Label(hr, textvariable=state["hint_var"], foreground="#777")
            state["hint"].pack(side="left")
        else:
            state["hint_var"] = tk.StringVar(value="")

        def refresh_hint(*_a: Any) -> None:
            parts: List[str] = []
            for label, y, mo, d in (("从", yf_var.get(), mf_var.get(), df_var.get()),
                                    ("到", yt_var.get(), mt_var.get(), dt_var.get())):
                if y == ANY:
                    parts.append(f"{label} 不限")
                    continue
                txt = y
                if mo != ANY:
                    txt += f"-{int(mo):02d}"
                    if d != ANY:
                        txt += f"-{int(d):02d}"
                parts.append(f"{label} {txt}")
            msgs: List[str] = []
            try:
                rng = parse_range({"date_from": value_from(yf_var.get(), mf_var.get(), df_var.get()),
                                   "date_to": value_from(yt_var.get(), mt_var.get(), dt_var.get())})
                msgs.append(f"生效范围 {rng.describe()}" if rng.active else "不限发布时间")
                a = parse_user_date(value_from(yf_var.get(), mf_var.get(), df_var.get()), mode="start")
                b = parse_user_date(value_from(yt_var.get(), mt_var.get(), dt_var.get()), mode="end")
                bad = bool(a and b and a >= b)
            except ValueError:
                bad = True
            state["error"][0] = bad
            if bad:
                msgs.append("⚠ 起始时间不早于结束时间，不会做时间筛选")
            state["hint_var"].set("\u3000".join(parts) + "\u3000—\u3000" + "；".join(msgs))
            if "hint" in state:
                state["hint"].configure(foreground="#c00" if bad else "#777")
            if on_change is not None:
                on_change()

        def make_sync(yv, mv, dv, m_cb, d_cb):
            def sync(*_a: Any) -> None:
                year_chosen = yv.get() != ANY
                month_chosen = year_chosen and mv.get() != ANY
                m_cb.configure(state="readonly" if year_chosen else "disabled")
                d_cb.configure(state="readonly" if month_chosen else "disabled")
                if not year_chosen and mv.get() != ANY:
                    mv.set(ANY)
                if year_chosen and not month_chosen and dv.get() != ANY:
                    dv.set(ANY)
                vals = [ANY]
                if month_chosen:
                    vals += [str(i) for i in range(1, days_in(int(yv.get()), int(mv.get())) + 1)]
                d_cb.configure(values=vals)
                if dv.get() not in vals:
                    dv.set(ANY)
            return sync

        def make_refresh_days(yv, mv, dv, d_cb):
            def refresh(*_a: Any) -> None:
                if yv.get() == ANY or mv.get() == ANY:
                    return
                vals = [ANY] + [str(i) for i in range(1, days_in(int(yv.get()), int(mv.get())) + 1)]
                d_cb.configure(values=vals)
                if dv.get() not in vals:
                    dv.set(ANY)
            return refresh

        for yv, mv, dv, m_cb, d_cb in ((yf_var, mf_var, df_var, mf_cb, df_cb),
                                       (yt_var, mt_var, dt_var, mt_cb, dt_cb)):
            refresh_days = make_refresh_days(yv, mv, dv, d_cb)
            sync = make_sync(yv, mv, dv, m_cb, d_cb)

            def on_year(*_a: Any, s=sync, r=refresh_days) -> None:
                s()
                r()
                # 年月日任一变化都要重算提示（进而触发 on_change）。
                # 早期只在"日"上挂了 trace，导致改年/改月不刷新 -> 检索页必须选到
                # 具体某天才生效，用户改了年份却看不到结果变化（实测踩到）。
                refresh_hint()

            def on_month_change(*_a: Any, r=refresh_days, s=sync) -> None:
                # 刻意**不**自动填"日"：填了就没法表达"整个 7 月"（日=不限 才是整月语义）
                r()
                s()
                refresh_hint()

            yv.trace_add("write", on_year)
            mv.trace_add("write", on_month_change)
            dv.trace_add("write", refresh_hint)
            sync()
            refresh_days()

        def set_values(f: Any, t: Any) -> None:
            a, b, c = parse_cfg(f)
            yf_var.set(str(a) if a else ANY)
            mf_var.set(str(b) if b else ANY)
            df_var.set(str(c) if c else ANY)
            a, b, c = parse_cfg(t)
            yt_var.set(str(a) if a else ANY)
            mt_var.set(str(b) if b else ANY)
            dt_var.set(str(c) if c else ANY)
            refresh_hint()

        state.update({
            "from_value": lambda: value_from(yf_var.get(), mf_var.get(), df_var.get()),
            "to_value": lambda: value_from(yt_var.get(), mt_var.get(), dt_var.get()),
            "set_values": set_values,
            "refresh_hint": refresh_hint,
            "vars": (yf_var, mf_var, df_var, yt_var, mt_var, dt_var),
        })
        # 初始算一次提示，但**延后**到事件循环：on_change 回调（例如检索页的
        # run_search）在调用本函数时可能还没定义完，立即调用会 NameError
        row.after_idle(refresh_hint)
        return state


    # ==================================================================================
    # 标签页 1：爬取
    # ==================================================================================
    tab_crawl = ttk.Frame(nb, padding=6)
    nb.add(tab_crawl, text="　爬取　")

    # 布局按「检索什么 → 留下什么 → 怎么爬」三段分组。
    # 这样分组的原因：前两段决定"要不要这张图"，第三段决定"跑多久、存多大"。
    # 混排会让人误以为勾"点赞≥1000"能爬到更多好图，其实只是把已拿到的筛掉一部分。

    def _section(parent: tk.Misc, title: str, hint: str = "") -> ttk.Frame:
        """一段带标题的分组框，返回可往里放控件的容器。"""
        box = ttk.LabelFrame(parent, text=f" {title} ", padding=(8, 4))
        box.pack(fill="x", pady=(0, 6))
        if hint:
            ttk.Label(box, text=hint, foreground="#777").pack(anchor="w", pady=(0, 2))
        return box

    # ============================ 第一段：检索什么 ============================
    sec_scope = _section(tab_crawl, "1. 检索什么",
                         "决定去 pixiv 搜什么，会改变能拿到的总量上限")

    # 检索范围：主检索方式用单选（避免"关键词+画师 叠加"的语义歧义）
    scope_row = ttk.Frame(sec_scope)
    scope_row.pack(fill="x", pady=1)
    ttk.Label(scope_row, text="检索范围：", width=10, anchor="w").pack(side="left")
    scope_var = tk.StringVar(value=str(cfg.get("scope") or "keyword"))
    for _val, _txt in (("keyword", "关键词"), ("artist", "画师ID"),
                       ("bookmarks", "收藏夹"), ("both", "两者都要")):
        ttk.Radiobutton(scope_row, text=_txt, value=_val, variable=scope_var,
                        command=lambda: _on_scope()).pack(side="left", padx=(0, 6))

    kw_row = ttk.Frame(sec_scope)
    kw_row.pack(fill="x", pady=1)
    ttk.Label(kw_row, text="关键词：", width=10, anchor="w").pack(side="left")
    kw_var = tk.StringVar()
    kw_entry = ttk.Entry(kw_row, textvariable=kw_var, width=34)
    kw_entry.pack(side="left")
    kw_entry.focus_set()
    ttk.Label(kw_row, text="（多个关键词用空格分隔）", foreground="#777").pack(
        side="left", padx=(6, 0))

    aid_row = ttk.Frame(sec_scope)
    aid_row.pack(fill="x", pady=1)
    ttk.Label(aid_row, text="画师ID：", width=10, anchor="w").pack(side="left")
    crawl_aid_var = tk.StringVar()
    crawl_aid_entry = ttk.Entry(aid_row, textvariable=crawl_aid_var, width=34)
    crawl_aid_entry.pack(side="left")
    ttk.Button(aid_row, text="从追更清单选…",
               command=lambda: pick_from_follow_list()).pack(side="left", padx=(6, 0))
    ttk.Label(aid_row, text="纯数字 ID 或主页链接，逗号分隔", foreground="#777").pack(
        side="left", padx=(6, 0))

    bm_row = ttk.Frame(sec_scope)
    bm_row.pack(fill="x", pady=1)
    ttk.Label(bm_row, text="收藏夹：", width=10, anchor="w").pack(side="left")
    bm_uid_var = tk.StringVar()
    bm_uid_entry = ttk.Entry(bm_row, textvariable=bm_uid_var, width=20)
    bm_uid_entry.pack(side="left")
    bm_hide_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(bm_row, text="私密收藏", variable=bm_hide_var).pack(side="left", padx=(6, 0))
    ttk.Label(bm_row, text="（私密仅本人可用，需 OAuth 登录）", foreground="#777").pack(
        side="left", padx=(6, 0))
    ttk.Label(bm_row, text="　收藏标签：").pack(side="left", padx=(8, 0))
    bm_tag_var = tk.StringVar()
    bm_tag_entry = ttk.Entry(bm_row, textvariable=bm_tag_var, width=12)
    bm_tag_entry.pack(side="left")

    scope_hint_var = tk.StringVar(value="")
    scope_hint = ttk.Label(sec_scope, textvariable=scope_hint_var, foreground="#777",
                           justify="left")
    scope_hint.pack(anchor="w", pady=(2, 0))

    def _on_scope() -> None:
        """切换检索范围时，把该方式的说明与注意事项显示出来。"""
        s = scope_var.get()
        if s == "keyword":
            scope_hint_var.set("在 pixiv 按关键词搜（标签部分匹配）。单一排序约 6180 条上限，"
                               "勾「深度」可翻倍。")
            crawl_aid_entry.state(["disabled"])
            _bm_entries_state("disabled")
        elif s == "artist":
            scope_hint_var.set("直接取这些画师的全部作品（走画师作品接口）："
                               "不受 6180 翻页上限、也不受「前排被 AI 占满」影响。"
                               "代价是逐个作品取详情，较慢。")
            crawl_aid_entry.state(["!disabled"])
            _bm_entries_state("disabled")
        elif s == "bookmarks":
            scope_hint_var.set("爬取某用户收藏夹里的作品（增量：已下载的自动跳过）。"
                               "收藏夹简表自带标签/分级/AI，筛选在列表阶段完成，省详情请求。"
                               "公开收藏任何登录态可用；私密需 OAuth 登录。")
            crawl_aid_entry.state(["disabled"])
            _bm_entries_state("!disabled")
        else:
            scope_hint_var.set("在关键词结果里只留这些画师。注意：交集可能很少甚至为空 ——"
                               "关键词是标签部分匹配，某画师的作品不一定带这个标签。")
            crawl_aid_entry.state(["!disabled"])
            _bm_entries_state("disabled")

    def _bm_entries_state(mode: str) -> None:
        for _w in (bm_uid_var, bm_hide_var, bm_tag_var):
            pass
        for _e in (bm_uid_entry, bm_tag_entry):
            _e.state([mode])

    def pick_from_follow_list() -> None:
        """从「画师追更」清单里挑画师，省得手抄 ID。"""
        store = None
        try:
            import importlib.util as _ilu
            _base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
                else Path(__file__).resolve().parent
            _p = next((c / "artists.py" for c in (_base, _base / "_internal")
                       if (c / "artists.py").is_file()), None)
            if _p is not None and _p.is_file():
                _s = _ilu.spec_from_file_location("pixiv_artists2", _p)
                _m = _ilu.module_from_spec(_s)
                sys.modules["pixiv_artists2"] = _m
                _s.loader.exec_module(_m)
                store = _m.ArtistStore(L["lib"].root / _m.ARTISTS_FILENAME)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("读取追更清单失败", f"{type(exc).__name__}: {exc}")
            return
        if store is None or not store.artists:
            messagebox.showinfo("追更清单是空的",
                                "还没有订阅任何画师。可以先去命令行添加：\n"
                                "python pixiv_crawler.py follow add <画师ID>\n\n"
                                "或在下面的输入框直接填画师 ID。")
            return
        dlg = tk.Toplevel(root)
        dlg.title("选择画师")
        dlg.transient(root)
        dlg.grab_set()
        ttk.Label(dlg, text="从追更清单里选（可多选，Ctrl/Shift）：",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        lb = tk.Listbox(dlg, selectmode="extended", width=46, height=12,
                        font=("Microsoft YaHei UI", 9), exportselection=False)
        lb.pack(padx=12)
        for a in store.artists:
            lb.insert("end", f"{a.label()}")
        opts = tk.BooleanVar(value=True)

        def use() -> None:
            sel = [store.artists[i].artist_id for i in lb.curselection()]
            if not sel:
                messagebox.showinfo("提示", "没有选中任何画师")
                return
            txt = ", ".join(sel)
            if opts.get():
                crawl_aid_var.set(txt)          # 替换
            else:
                old = crawl_aid_var.get().strip()
                crawl_aid_var.set((old + ", " + txt).strip(", ") if old else txt)
            dlg.destroy()

        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=12, pady=10)
        ttk.Checkbutton(row, text="替换现有内容（不勾则追加）", variable=opts).pack(side="left")
        ttk.Button(row, text="使用", command=use).pack(side="right")
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="right", padx=6)

    # AI 生成 + 内容分级：都用单选/勾选，保持同一页的交互语言一致
    ai_row = ttk.Frame(sec_scope)
    ai_row.pack(fill="x", pady=1)
    ttk.Label(ai_row, text="AI 生成：", width=10, anchor="w").pack(side="left")
    ai_mode_var = tk.StringVar(value=str(cfg.get("ai_mode") or "all"))
    for _val, _txt in (("all", "不限"), ("exclude", "排除 AI"), ("only", "只要 AI")):
        ttk.Radiobutton(ai_row, text=_txt, value=_val, variable=ai_mode_var).pack(
            side="left", padx=(0, 6))
    ttk.Label(ai_row, text="数据来自搜索列表自带字段，不额外消耗请求",
              foreground="#777").pack(side="left", padx=(6, 0))

    lv_row = ttk.Frame(sec_scope)
    lv_row.pack(fill="x", pady=1)
    ttk.Label(lv_row, text="内容分级：", width=10, anchor="w").pack(side="left")
    _init_levels = []
    if not parse_bool(cfg.get("skip_r18"), True):
        _init_levels.extend([0, 1])
        if not parse_bool(cfg.get("skip_r18g"), True):
            _init_levels.append(2)
    else:
        _init_levels.append(0)
    lv_all_var = tk.BooleanVar(value=0 in _init_levels)
    lv_r18_var = tk.BooleanVar(value=1 in _init_levels)
    lv_r18g_var = tk.BooleanVar(value=2 in _init_levels)
    _lv_vars = {0: lv_all_var, 1: lv_r18_var, 2: lv_r18g_var}
    r18_hint_var = tk.StringVar(value="")
    r18_hint = None
    lv_checks = {}
    for _lvl, _text in ((0, "全年龄"), (1, "R-18"), (2, "R-18G（猎奇）")):
        cb = ttk.Checkbutton(lv_row, text=_text, variable=_lv_vars[_lvl], command=lambda: None)
        cb.pack(side="left", padx=(0, 4))
        lv_checks[_lvl] = cb
    ttk.Label(lv_row, text="可自由组合（6 种）", foreground="#777").pack(side="left", padx=(6, 0))

    def picked_levels() -> List[int]:
        return [lvl for lvl in (0, 1, 2) if _lv_vars[lvl].get()]

    def update_level_hint() -> None:
        """实时回显：这种勾选会用什么服务器模式、"全不勾"时给警告。"""
        lvls = picked_levels()
        if not lvls:
            r18_hint_var.set("⚠ 一个都没勾 = 不会爬取任何内容")
            if r18_hint is not None:
                r18_hint.configure(foreground="#c00")
            return
        mode, allowed = r18_plan(lvls)
        r18_hint_var.set("→ 收 " + "、".join(R18_LEVEL_NAMES[x] for x in allowed)
                         + f"　（服务器 mode={mode}）")
        if r18_hint is not None:
            r18_hint.configure(foreground="#777")

    for _cb in lv_checks.values():
        _cb.configure(command=update_level_hint)

    # ============================ 第二段：留下什么 ============================
    sec_keep = _section(tab_crawl, "2. 留下什么",
                        "只影响「从已拿到的结果里保留哪些」，不会让能爬到的总数变多")

    date_range = build_date_range(sec_keep, cfg.get("date_from"), cfg.get("date_to"),
                                  prefix="发布时间：")
    date_error = date_range["error"]

    pop_row = ttk.Frame(sec_keep)
    pop_row.pack(fill="x", pady=(2, 0))
    ttk.Label(pop_row, text="点赞数≥", width=10, anchor="w").pack(side="left")
    min_likes_var = tk.IntVar(value=as_int(cfg.get("min_likes"), 0))
    ttk.Spinbox(pop_row, from_=0, to=1000000, increment=100, textvariable=min_likes_var,
                width=8).pack(side="left")
    ttk.Label(pop_row, text="收藏数≥").pack(side="left", padx=(10, 0))
    min_bm_var = tk.IntVar(value=as_int(cfg.get("min_bookmarks"), 0))
    ttk.Spinbox(pop_row, from_=0, to=1000000, increment=100, textvariable=min_bm_var,
                width=8).pack(side="left")
    ttk.Label(pop_row, text="（两者同时满足才留下；填了会多花请求，因为搜索结果不带这两个数）",
              foreground="#777").pack(side="left", padx=(8, 0))

    # ============================ 第三段：怎么爬 ============================
    sec_run = _section(tab_crawl, "3. 怎么爬 / 存成什么",
                       "决定跑多久、占多大空间；不影响「留下哪些图」")

    strat_row = ttk.Frame(sec_run)
    strat_row.pack(fill="x", pady=1)
    ttk.Label(strat_row, text="搜索策略：", width=10, anchor="w").pack(side="left")
    strat_var = tk.StringVar(value="normal")
    ttk.Radiobutton(strat_row, text="普通（翻 N 页）", value="normal",
                    variable=strat_var).pack(side="left", padx=(0, 6))
    ttk.Radiobutton(strat_row, text="深度（最新+最早）", value="deep",
                    variable=strat_var).pack(side="left", padx=(0, 6))
    ttk.Radiobutton(strat_row, text="全量（按时间段切分）", value="full",
                    variable=strat_var).pack(side="left", padx=(0, 6))
    strat_hint_var = tk.StringVar(value="")
    ttk.Label(sec_run, textvariable=strat_hint_var, foreground="#777",
              justify="left").pack(anchor="w")

    def _on_strategy() -> None:
        s = strat_var.get()
        if s == "normal":
            strat_hint_var.set("单一排序翻 N 页。实测单一排序约 6180 条封顶（第 104 页起返回空）。")
        elif s == "deep":
            strat_hint_var.set("组合「最新」与「最早」两个不重叠区段。实测匿名 598→1198，"
                               "登录后 6180→10920（+73%）。")
        else:
            strat_hint_var.set("按时间段递归切分，突破单一查询的翻页上限，把关键词结果尽量取全。"
                               "很慢、占空间大 —— 强烈建议先点下面的「预估」。")
        deep_var.set(s == "deep")           # 兼容既有传参
        full_var.set(s == "full")

    deep_var = tk.BooleanVar(value=False)
    full_var = tk.BooleanVar(value=False)

    page_row = ttk.Frame(sec_run)
    page_row.pack(fill="x", pady=1)
    ttk.Label(page_row, text="页数：", width=10, anchor="w").pack(side="left")
    pages_var = tk.IntVar(value=int(cfg.get("pages") or 1))
    ttk.Spinbox(page_row, from_=1, to=200, textvariable=pages_var, width=6).pack(side="left")
    ttk.Label(page_row, text="排序：").pack(side="left", padx=(10, 0))
    order_var = tk.StringVar(value=str(cfg.get("order") or "date"))
    ttk.Combobox(page_row, textvariable=order_var, width=9, state="readonly",
                 values=["date", "popular", "old"]).pack(side="left")
    ttk.Label(page_row, text="每作品最多").pack(side="left", padx=(10, 0))
    mpw_var = tk.IntVar(value=as_int(cfg.get("max_pages_per_work"), 0))
    ttk.Spinbox(page_row, from_=0, to=1000, textvariable=mpw_var, width=5).pack(side="left")
    ttk.Label(page_row, text="页（0=全部）", foreground="#777").pack(side="left", padx=(2, 0))
    ttk.Label(page_row, text="　本次上限").pack(side="left")
    limit_var = tk.IntVar(value=as_int(cfg.get("max_works_per_run"), 0))
    ttk.Spinbox(page_row, from_=0, to=1000000, increment=100, textvariable=limit_var,
                width=8).pack(side="left")
    ttk.Label(page_row, text="个作品（0=不限）", foreground="#777").pack(side="left", padx=(2, 0))

    ugo_row = ttk.Frame(sec_run)
    ugo_row.pack(fill="x", pady=1)
    ttk.Label(ugo_row, text="动图：", width=10, anchor="w").pack(side="left")
    ugo_mode_var = tk.StringVar(value="none" if str(cfg.get("ugoira_format") or "none") == "none"
                               else str(cfg.get("ugoira_format")))
    ttk.Radiobutton(ugo_row, text="不收", value="none", variable=ugo_mode_var).pack(
        side="left", padx=(0, 6))
    ttk.Radiobutton(ugo_row, text="只收原始 zip", value="zip", variable=ugo_mode_var).pack(
        side="left", padx=(0, 6))
    ttk.Radiobutton(ugo_row, text="转成", value="convert", variable=ugo_mode_var).pack(side="left")
    ugo_fmt_var = tk.StringVar(value=str(cfg.get("ugoira_format") or "none"))
    ttk.Combobox(ugo_row, textvariable=ugo_fmt_var, width=7, state="readonly",
                 values=["webp", "gif", "mp4", "webm"]).pack(side="left", padx=(2, 6))
    ugo_keep_var = tk.BooleanVar(value=parse_bool(cfg.get("ugoira_keep_zip"), True))
    ttk.Checkbutton(ugo_row, text="保留原始 zip", variable=ugo_keep_var).pack(side="left")
    ttk.Label(ugo_row, text="（转格式需要 ffmpeg）", foreground="#777").pack(
        side="left", padx=(6, 0))

    def _on_ugo_mode() -> None:
        """把三档单选翻译成既有的 ugo_fmt_var（none / 具体格式）。"""
        m = ugo_mode_var.get()
        if m == "none":
            ugo_fmt_var.set("none")
        elif m == "zip":
            ugo_fmt_var.set("none")          # 只收 zip：格式仍是 none，靠 keep_zip 区分
            ugo_keep_var.set(True)
        else:
            if ugo_fmt_var.get() == "none":
                ugo_fmt_var.set("webp")
    for _w in ugo_row.winfo_children():
        if isinstance(_w, ttk.Radiobutton):
            _w.configure(command=_on_ugo_mode)

    mid = ttk.Frame(tab_crawl)
    mid.pack(fill="both", expand=True, padx=0)
    # 高度别设太大：爬取页上面有 3 段筛选控件，日志若默认 16 行(约240px)+筛选+按钮行
    # 会超过窗口高度；好在按钮行已用 side="bottom" 固定到底部（见 crawl_btns），
    # 超出的部分只会压缩日志区，按钮始终可见（实测踩过"按钮被挤成 1px"）。
    log = tk.Text(mid, height=10, wrap="none", font=("Consolas", 9))
    log.grid(row=0, column=0, sticky="nsew")
    sb = ttk.Scrollbar(mid, command=log.yview)
    sb.grid(row=0, column=1, sticky="ns")
    log.configure(yscrollcommand=sb.set)
    mid.rowconfigure(0, weight=1)
    mid.columnconfigure(0, weight=1)

    status_var = tk.StringVar(value="就绪")

    def logln(msg: str) -> None:
        log.insert("end", msg + "\n")
        log.see("end")

    update_level_hint()   # 初始化分级回显

    # ==================================================================================
    # 标签页 2：追更（订阅画师清单 + 一键同步）
    # ==================================================================================
    tab_follow = ttk.Frame(nb, padding=6)
    nb.add(tab_follow, text="　追更　")

    try:
        _follow_store = artists_mod.ArtistStore(L["lib"].root / artists_mod.ARTISTS_FILENAME)
    except Exception:  # noqa: BLE001
        _follow_store = None

    frow = ttk.Frame(tab_follow)
    frow.pack(fill="x")
    ttk.Label(frow, text="订阅的画师（增量追更：只下载上次以后的新作品）",
              foreground="#666").pack(side="left")
    ttk.Button(frow, text="添加画师…", command=lambda: add_follow_dialog()).pack(
        side="left", padx=(10, 0))
    ttk.Button(frow, text="移除选中", command=lambda: remove_follows()).pack(side="left", padx=4)
    dry_run_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(frow, text="干跑（只报新增不下载）", variable=dry_run_var).pack(
        side="left", padx=(8, 0))
    follow_status_var = tk.StringVar(value="就绪")
    ttk.Label(frow, textvariable=follow_status_var, foreground="#777").pack(
        side="left", padx=(10, 0))

    fcols = ("name", "id", "last", "note")
    fheads = ("画师", "ID", "上次追更", "备注")
    fwidths = (200, 110, 170, 200)
    ftree = ttk.Treeview(tab_follow, columns=fcols, show="headings", height=12)
    for c, w, h in zip(fcols, fwidths, fheads):
        ftree.heading(c, text=h)
        ftree.column(c, width=w, anchor="w")
    ftree.pack(fill="both", expand=True, pady=(6, 0))
    fvsb = ttk.Scrollbar(tab_follow, orient="vertical", command=ftree.yview)
    fvsb.pack(side="right", fill="y")
    ftree.configure(yscrollcommand=fvsb.set)

    fbtns = ttk.Frame(tab_follow, padding=(0, 8, 0, 0))
    fbtns.pack(fill="x")
    ttk.Button(fbtns, text="同步选中", command=lambda: start_follow_sync(only_selected=True)).pack(side="left")
    ttk.Button(fbtns, text="同步全部（增量下载）",
               command=lambda: start_follow_sync(only_selected=False)).pack(side="left", padx=6)
    ttk.Button(fbtns, text="停止", command=lambda: stop_follow()).pack(side="left", padx=6)
    ttk.Button(fbtns, text="打开追更清单文件",
               command=lambda: open_path(
                   L["lib"].root / artists_mod.ARTISTS_FILENAME)).pack(side="left", padx=6)
    ttk.Label(fbtns, textvariable=follow_status_var, foreground="#666").pack(side="right")

    # 追更页进度行
    f_prog_row = ttk.Frame(tab_follow, padding=(0, 4, 0, 0))
    f_prog_row.pack(fill="x")
    f_prog = ttk.Progressbar(f_prog_row, maximum=100, value=0, length=320)
    f_prog.pack(side="left", fill="x", expand=True)
    f_prog_var = tk.StringVar(value="就绪")
    ttk.Label(f_prog_row, textvariable=f_prog_var, foreground="#666", width=50,
              anchor="w").pack(side="left", padx=(8, 0))

    f_log = tk.Text(tab_follow, height=8, wrap="none", font=("Consolas", 9))
    f_log.pack(fill="x", pady=(8, 0))

    def flogln(msg: str) -> None:
        f_log.insert("end", str(msg) + "\n")
        f_log.see("end")

    def refresh_follows() -> None:
        if artists_mod is None:
            follow_status_var.set("缺少 artists.py，追更不可用")
            return
        global _follow_store
        _follow_store = artists_mod.ArtistStore(L["lib"].root / artists_mod.ARTISTS_FILENAME)
        ftree.delete(*ftree.get_children())
        for a in _follow_store.artists:
            ftree.insert("", "end", values=(
                a.name or f"id={a.artist_id}", a.artist_id,
                (a.last_sync[:19].replace("T", " ") if a.last_sync else "尚未追更"),
                a.note))
        follow_status_var.set(f"共 {len(_follow_store.artists)} 位画师")

    def add_follow_dialog() -> None:
        dlg = tk.Toplevel(root)
        dlg.title("订阅画师")
        dlg.transient(root)
        dlg.grab_set()
        ttk.Label(dlg, text="画师 ID 或主页链接（可用逗号分隔多个）：",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        var = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=var, width=56)
        ent.pack(padx=12, fill="x")
        name_var = tk.StringVar()
        ttk.Label(dlg, text="画师名（可留空，同步时自动补）：").pack(anchor="w", padx=12, pady=(8, 2))
        ttk.Entry(dlg, textvariable=name_var, width=56).pack(padx=12, fill="x")
        info = tk.StringVar(value="")
        ttk.Label(dlg, textvariable=info, foreground="#c00").pack(anchor="w", padx=12, pady=(6, 0))

        def ok() -> None:
            if artists_mod is None:
                info.set("缺少 artists.py")
                return
            text = var.get().strip()
            ids = [artists_mod.parse_artist_ref(x) for x in re.split(r"[\s,，]+", text) if x.strip()]
            ids = [i for i in ids if i]
            if not ids:
                info.set("看不懂输入：支持纯数字ID或 https://www.pixiv.net/users/12345")
                return
            added = 0
            name = (name_var.get() or "").strip()
            for aid in ids:
                a, is_new = _follow_store.add(aid, name=name)
                added += 1 if is_new else 0
            _follow_store.save()
            dlg.destroy()
            refresh_follows()
            flogln(f"已添加 {added} 位，共 {len(_follow_store.artists)} 位画师")
            if added:
                start_follow_sync(dry_run=True)   # 加完顺手看增量，不真正下载

        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=12, pady=10)
        ttk.Button(row, text="添加并预览增量", command=ok).pack(side="left")
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        ent.bind("<Return>", lambda _e: ok())
        ent.focus_set()

    def remove_follows() -> None:
        sel = ftree.selection()
        if not sel:
            flogln("请先在清单里选中要移除的画师")
            return
        removed = 0
        for item in sel:
            aid = ftree.item(item, "values")[1]
            if _follow_store.remove(aid):
                removed += 1
        if removed:
            _follow_store.save()
            flogln(f"已移除 {removed} 位画师")
        refresh_follows()

    def start_follow_sync(dry_run: bool = False, only_selected: bool = True) -> None:
        if L.get("busy"):
            flogln("正在执行其它任务，请稍候")
            return
        if artists_mod is None or _follow_store is None:
            flogln("缺少 artists.py")
            return
        if not _follow_store.artists:
            flogln("追更清单是空的，先添加画师")
            return
        if only_selected:
            sel = ftree.selection()
            if not sel:
                flogln("请先在清单里选中要同步的画师（或点「同步全部」）")
                return
            wanted = {ftree.item(i, "values")[1] for i in sel}
            targets = [a for a in _follow_store.artists if a.artist_id in wanted]
        else:
            targets = list(_follow_store.artists)
        # 记录本次同步画师，供完成后跳到检索页自动筛选展示
        L["last_sync_artists"] = [a.artist_id for a in targets]
        L["goto_search_after_follow"] = not dry
        L["busy"] = True
        L["stop_evt"] = threading.Event()
        follow_status_var.set("正在同步…")
        f_prog.configure(value=0)
        f_prog_var.set(f"准备同步 {len(targets)} 位画师…")
        flogln("")
        flogln("=" * 30 + " 开始同步 " + "=" * 30)
        dry = bool(dry_run_var.get()) or dry_run
        snap = dict(L["cfg"])
        started = time.time()

        def on_progress(kw: Dict[str, Any]) -> None:
            msg_q.put(("__FPROG__", kw))

        def worker() -> None:
            try:
                run_follow_sync(snap, L["lib"], _follow_store, targets,
                                dry_run=dry, order=str(snap.get("order") or "date"),
                                stop_event=L["stop_evt"], progress_cb=on_progress,
                                log=lambda m: msg_q.put(("[follow]", m)), started=started)
                msg_q.put(("__FOLLOW_DONE__", None))
            except Exception as exc:  # noqa: BLE001
                msg_q.put(("[follow]", f"[error] {type(exc).__name__}: {exc}"))
                msg_q.put(("__FOLLOW_DONE__", None))
        threading.Thread(target=worker, daemon=True).start()

    def stop_follow() -> None:
        evt = L.get("stop_evt")
        if not L.get("busy") or evt is None:
            follow_status_var.set("当前没有运行中的同步")
            return
        evt.set()
        follow_status_var.set("已请求停止，正在保存…（下次同步自动续跑）")
        f_prog_var.set("正在停止…")

    def _render_follow_progress(kw: Dict[str, Any]) -> None:
        stage = kw.get("stage")
        done = int(kw.get("done") or 0)
        total = int(kw.get("total") or 1)
        detail = str(kw.get("detail") or "")
        if total > 0:
            f_prog.configure(maximum=total, value=done)
        pct = int(done * 100 / total) if total else 0
        if stage == "follow":
            f_prog_var.set(f"画师 {done}/{total}（{pct}%）　{detail[:36]}")
        else:  # artist / artist_dl
            f_prog_var.set(f"{detail[:20]}　作品 {done}/{total}（{pct}%）")

    refresh_follows()

    # ==================================================================================
    # 标签页 3：图库检索
    # ==================================================================================
    tab_lib = ttk.Frame(nb, padding=6)
    nb.add(tab_lib, text="　图库检索　")

    srow1 = ttk.Frame(tab_lib, padding=(0, 0, 0, 4))
    srow1.pack(fill="x")
    ttk.Label(srow1, text="检索词：").pack(side="left")
    q_text_var = tk.StringVar()
    qe = ttk.Entry(srow1, textvariable=q_text_var, width=26)
    qe.pack(side="left")
    ttk.Label(srow1, text="画师：").pack(side="left", padx=(8, 0))
    q_author_var = tk.StringVar()
    ttk.Entry(srow1, textvariable=q_author_var, width=14).pack(side="left")
    # 画师 ID：精确筛选（可填多个，用逗号分隔；支持主页链接）
    ttk.Label(srow1, text="画师ID：").pack(side="left", padx=(8, 0))
    q_aid_var = tk.StringVar()
    q_aid_entry = ttk.Entry(srow1, textvariable=q_aid_var, width=18)
    q_aid_entry.pack(side="left")
    ttk.Label(srow1, text="排序：").pack(side="left", padx=(8, 0))
    q_sort_var = tk.StringVar(value="default")
    ttk.Combobox(srow1, textvariable=q_sort_var, width=10, state="readonly",
                 values=["default", "date", "likes", "bookmarks", "size"]).pack(side="left")
    ttk.Label(srow1, text="　上限 ").pack(side="left")
    q_limit_var = tk.IntVar(value=1000)
    ttk.Spinbox(srow1, from_=0, to=100000, increment=100, textvariable=q_limit_var,
                width=7).pack(side="left")
    ttk.Label(srow1, text=" 条（0=不限）", foreground="#777").pack(side="left")

    srow2 = ttk.Frame(tab_lib, padding=(0, 0, 0, 4))
    srow2.pack(fill="x")
    ttk.Label(srow2, text="点赞数 ≥ ").pack(side="left")
    q_likes_var = tk.IntVar(value=0)
    ttk.Spinbox(srow2, from_=0, to=1000000, increment=100, textvariable=q_likes_var,
                width=8).pack(side="left")
    ttk.Label(srow2, text="收藏数 ≥ ").pack(side="left", padx=(8, 0))
    q_bm_var = tk.IntVar(value=0)
    ttk.Spinbox(srow2, from_=0, to=1000000, increment=100, textvariable=q_bm_var,
                width=8).pack(side="left")

    ttk.Label(srow2, text="　分级：").pack(side="left")
    q_r18_var = tk.StringVar(value="hide")
    # 与爬取页的三个勾选框一一对应的六种组合
    for label, val in (("全年龄", "hide"), ("全年龄+R-18", "no_g"),
                       ("只有 R-18", "only_r18"), ("只有 R-18G", "only_g"),
                       ("全年龄+R-18G", "only_g_mix"), ("R-18+R-18G", "r18_all"),
                       ("全部", "all")):
        ttk.Radiobutton(srow2, text=label, value=val, variable=q_r18_var,
                        command=lambda: run_search()).pack(side="left")
    ttk.Label(srow2, text="　AI：").pack(side="left")
    q_ai_var = tk.StringVar(value="all")
    for label, val in (("不限", "all"), ("排除 AI", "exclude"), ("只要 AI", "only")):
        ttk.Radiobutton(srow2, text=label, value=val, variable=q_ai_var,
                        command=lambda: run_search()).pack(side="left")

    # 检索页的发布时间用与爬取页**完全相同**的年月日三级下拉
    q_date_range = build_date_range(tab_lib, "", "", prefix="发布时间：",
                                    on_change=lambda: run_search())

    srow3 = ttk.Frame(tab_lib, padding=(0, 0, 0, 4))
    srow3.pack(fill="x")
    ttk.Label(srow3, text="原有TAG筛选（可多选 Ctrl/Shift；默认要求全部命中）：").pack(side="left")
    tag_all_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(srow3, text="要求全部命中", variable=tag_all_var).pack(side="left", padx=(6, 0))
    ttk.Label(srow3, text="　快速过滤标签：").pack(side="left")
    tag_filter_var = tk.StringVar()
    # Combobox：输入时自动补全候选（如键入「原」→ 列出「原神」等已存在标签），
    # 选择或回车后按该标签过滤左侧列表
    tag_filter_cb = ttk.Combobox(srow3, textvariable=tag_filter_var, width=16)
    tag_filter_cb.pack(side="left")
    ttk.Label(srow3, text="（可输入过滤，或从候选里选）", foreground="#888").pack(side="left", padx=(6, 0))

    dtag_row = ttk.Frame(tab_lib, padding=(0, 0, 0, 4))
    dtag_row.pack(fill="x")
    ttk.Label(dtag_row, text="衍生TAG筛选（你加的那一层，含自动「第N次爬取」）：").pack(side="left")
    dtag_all_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(dtag_row, text="要求全部命中", variable=dtag_all_var).pack(side="left", padx=(6, 0))
    dtag_filter_var = tk.StringVar()
    ttk.Label(dtag_row, text="　快速过滤：").pack(side="left")
    dtag_filter_cb = ttk.Combobox(dtag_row, textvariable=dtag_filter_var, width=16)
    dtag_filter_cb.pack(side="left")
    ttk.Label(dtag_row, text="（可输入过滤，或从候选里选）", foreground="#888").pack(side="left", padx=(6, 0))
    ttk.Button(dtag_row, text="编辑衍生标签…",
               command=lambda: edit_dtags_dialog()).pack(side="left", padx=(10, 0))

    # ---- 画师ID筛选区（夹在原TAG与衍生TAG之间）----
    aid_row = ttk.Frame(tab_lib, padding=(0, 0, 0, 4))
    aid_row.pack(fill="x")
    ttk.Label(aid_row, text="画师ID筛选（可多选 Ctrl/Shift；选中的画师都会显示）：").pack(side="left")
    ttk.Label(aid_row, text="　快速过滤：").pack(side="left")
    aid_filter_var = tk.StringVar()
    aid_filter_cb = ttk.Combobox(aid_row, textvariable=aid_filter_var, width=16)
    aid_filter_cb.pack(side="left")
    ttk.Label(aid_row, text="（输入画师名/ID 过滤列表，或从候选里选）",
              foreground="#888").pack(side="left", padx=(6, 0))
    ttk.Button(aid_row, text="清除画师筛选",
               command=lambda: clear_aid_filter()).pack(side="left", padx=(10, 0))

    trow = ttk.Frame(tab_lib)
    trow.pack(fill="both", expand=True)
    tag_box = tk.Listbox(trow, selectmode="extended", width=32, height=12,
                         font=("Microsoft YaHei UI", 9), exportselection=False)
    tag_box.grid(row=0, column=0, sticky="nsw")
    tag_sb = ttk.Scrollbar(trow, command=tag_box.yview)
    tag_sb.grid(row=0, column=1, sticky="ns")
    tag_box.configure(yscrollcommand=tag_sb.set)
    ttk.Label(trow, text="原TAG", foreground="#888").grid(row=1, column=0, sticky="w")

    aid_box = tk.Listbox(trow, selectmode="extended", width=26, height=12,
                         font=("Microsoft YaHei UI", 9), exportselection=False)
    aid_box.grid(row=0, column=2, sticky="nsw", padx=(8, 0))
    aid_sb = ttk.Scrollbar(trow, command=aid_box.yview)
    aid_sb.grid(row=0, column=3, sticky="ns")
    aid_box.configure(yscrollcommand=aid_sb.set)
    ttk.Label(trow, text="画师（可多选）", foreground="#086").grid(row=1, column=2, sticky="w")

    dtag_box = tk.Listbox(trow, selectmode="extended", width=26, height=12,
                          font=("Microsoft YaHei UI", 9), exportselection=False)
    dtag_box.grid(row=0, column=4, sticky="nsw", padx=(8, 0))
    dtag_sb = ttk.Scrollbar(trow, command=dtag_box.yview)
    dtag_sb.grid(row=0, column=5, sticky="ns")
    dtag_box.configure(yscrollcommand=dtag_sb.set)
    ttk.Label(trow, text="衍生TAG（可编辑）", foreground="#06c").grid(row=1, column=4, sticky="w")

    res_frame = ttk.Frame(trow)
    res_frame.grid(row=0, column=6, sticky="nsew", padx=(8, 0))
    trow.columnconfigure(6, weight=1)
    trow.rowconfigure(0, weight=1)

    cols = ("id", "title", "author", "tags", "dtags", "likes", "bm", "date", "size", "file")
    heads = ("作品ID", "标题", "画师", "原TAG", "衍生TAG", "点赞", "收藏", "发布时间", "大小", "文件")
    widths = (90, 200, 130, 240, 180, 60, 60, 96, 76, 300)
    tree = ttk.Treeview(res_frame, columns=cols, show="headings")
    for c, w, h in zip(cols, widths, heads):
        tree.heading(c, text=h)
        tree.column(c, width=w, anchor="w")
    tree.grid(row=0, column=0, sticky="nsew")
    vsb = ttk.Scrollbar(res_frame, orient="vertical", command=tree.yview)
    vsb.grid(row=0, column=1, sticky="ns")
    hsb = ttk.Scrollbar(res_frame, orient="horizontal", command=tree.xview)
    hsb.grid(row=1, column=0, sticky="ew")
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    res_frame.rowconfigure(0, weight=1)
    res_frame.columnconfigure(0, weight=1)

    # ---- 预览面板：选中行即显示缩略图（PNG/GIF）+ 元信息 ----
    # tkinter 的 PhotoImage 只支持 PNG/GIF，JPG/WebP 等显示占位提示（零依赖原则）。
    preview_frame = ttk.LabelFrame(tab_lib, text="预览（单击行查看；双击打开）", padding=6)
    preview_frame.pack(fill="x", pady=(6, 0))
    preview_row = ttk.Frame(preview_frame)
    preview_row.pack(fill="x")
    preview_img = ttk.Label(preview_row, text="（选择一行后这里显示缩略图；PNG/GIF 可预览，JPG 等请双击打开）",
                            foreground="#888", anchor="w", width=60)
    preview_img.pack(side="left", fill="x", expand=True)
    preview_info_var = tk.StringVar(value="")
    ttk.Label(preview_row, textvariable=preview_info_var, foreground="#333",
              justify="left", width=46, anchor="w").pack(side="left", padx=(8, 0))

    def refresh_preview(*_a: Any) -> None:
        """选中行 -> 更新预览缩略图与元信息。"""
        sel = tree.selection()
        preview_info_var.set("")
        if not sel:
            preview_img.configure(image="", text="（选择一行后这里显示缩略图）")
            return
        rel = tree.item(sel[0], "values")[9]
        p = L["lib"].root / str(rel).replace("/", os.sep)
        vals = tree.item(sel[0], "values")
        # 元信息（ID 列可能带「×N页」标记，预览里还原纯 ID）
        info = (f"ID {str(vals[0]).split('×', 1)[0]}　{vals[1]}\n画师 {vals[2]}\n"
                f"发布于 {vals[7]}　大小 {vals[8]}\n文件 {rel}")
        preview_info_var.set(info)
        # 缩略图：
        #  · 装了 Pillow（推荐环境，已在用的那套 Python）→ 全格式（JPG/WebP/GIF/PNG…）都能预览
        #  · 没装 Pillow（其他纯 Python 环境）→ 退回 tkinter 原生，只支持 PNG/GIF
        # 两种情况都优雅降级，不会崩。
        try:
            fmt = str(p.suffix).lower()
            if not p.exists():
                preview_img.configure(image="", text="（文件不存在）")
                return
            tk_img = None
            try:
                from PIL import Image, ImageTk  # type: ignore
                with Image.open(native_path(p)) as im:
                    im = im.convert("RGB")
                    im.thumbnail((360, 220), Image.Resampling.LANCZOS)
                    tk_img = ImageTk.PhotoImage(im)
            except ImportError:
                # 无 Pillow：走 tkinter 原生，仅 PNG/GIF
                if fmt in (".png", ".gif"):
                    from tkinter import PhotoImage as _PhotoImage
                    tk_img = _PhotoImage(file=native_path(p))
                    w, h = tk_img.width(), tk_img.height()
                    while w > 360 or h > 220:
                        tk_img = tk_img.subsample(2, 2)
                        w, h = tk_img.width(), tk_img.height()
            if tk_img is not None:
                L["_preview_ref"] = tk_img        # 防止被 GC
                preview_img.configure(image=tk_img, text="")
            else:
                L.pop("_preview_ref", None)
                preview_img.configure(image="", text=f"（{fmt[1:].upper()}：本机预览库不可用，双击打开）")
        except Exception as exc:  # noqa: BLE001
            L.pop("_preview_ref", None)
            preview_img.configure(image="", text=f"（预览失败：{type(exc).__name__}）")

    tree.bind("<<TreeviewSelect>>", refresh_preview)
    tree.bind("<Double-1>", lambda _e: open_selected())

    lib_status_var = tk.StringVar(value="就绪")
    brow = ttk.Frame(tab_lib, padding=(0, 4, 0, 0))
    brow.pack(fill="x")
    ttk.Label(brow, textvariable=lib_status_var, foreground="#666").pack(side="left")

    def selected_tags() -> List[str]:
        return [tag_box.get(i) for i in tag_box.curselection()]

    def selected_dtags() -> List[str]:
        return [dtag_box.get(i) for i in dtag_box.curselection()]

    def load_tags() -> None:
        """把图库里的标签按出现次数填进左侧列表。"""
        hist = tag_histogram(L["recs"])
        keep = [t for t in selected_tags()]
        tag_box.delete(0, "end")
        kw = tag_filter_var.get().strip().lower()
        for t, n in hist:
            if kw and kw not in t.lower():
                continue
            tag_box.insert("end", f"{t}　({n})")
        for i in range(tag_box.size()):
            name = tag_box.get(i).rsplit("　(", 1)[0]
            if name in keep:
                tag_box.selection_set(i)
        lib_status_var.set(f"原TAG {len(hist)} 个，当前列出 {tag_box.size()} 个")

    def load_dtag_list() -> None:
        """把衍生标签（含自动「第N次爬取」）按出现次数填进列表。"""
        hist = dtag_histogram(L["recs"])
        keep = [t for t in selected_dtags()]
        dtag_box.delete(0, "end")
        kw = dtag_filter_var.get().strip().lower()
        for t, n in hist:
            if kw and kw not in t.lower():
                continue
            dtag_box.insert("end", f"{t}　({n})")
        for i in range(dtag_box.size()):
            name = dtag_box.get(i).rsplit("　(", 1)[0]
            if name in keep:
                dtag_box.selection_set(i)

    def _aid_selected_ids() -> List[str]:
        """画师列表当前选中的画师ID（列表项形如「画师名　(数)」，需要按索引查 records 拿 ID）。"""
        ids: List[str] = []
        for i in aid_box.curselection():
            label = aid_box.get(i)
            # 用 label 反向匹配 records：author 名相同 + 出现次数相同
            name = label.rsplit("　(", 1)[0]
            for r in L["recs"]:
                if str(r.get("author") or "").strip() == name:
                    aid = str(r.get("author_id") or "").strip()
                    if aid and aid not in ids:
                        ids.append(aid)
                        break
        return ids

    def load_aid_list() -> None:
        """把图库里的画师按作品数填进列表（画师名　(作品数)），并按过滤词过滤。"""
        sel_prev = _aid_selected_ids()
        aid_box.delete(0, "end")
        kw = aid_filter_var.get().strip().lower()
        for aid, name, n in author_histogram(L["recs"]):
            if kw and kw not in str(name).lower() and kw not in aid:
                continue
            aid_box.insert("end", f"{name}　({n})")
        # 恢复选中
        for i in range(aid_box.size()):
            label = aid_box.get(i)
            name = label.rsplit("　(", 1)[0]
            for r in L["recs"]:
                if str(r.get("author") or "").strip() == name and \
                        str(r.get("author_id") or "").strip() in sel_prev:
                    aid_box.selection_set(i)
                    break

    def clear_aid_filter() -> None:
        """清除画师筛选：清空列表选择、顶部输入框与过滤词。"""
        aid_box.selection_clear(0, "end")
        q_aid_var.set("")
        aid_filter_var.set("")
        aid_filter_cb.set("")
        load_aid_list()
        run_search()

    def _on_aid_select(*_a: Any) -> None:
        # 点选画师 → 同步到 q_aid_var → 检索
        ids = _aid_selected_ids()
        q_aid_var.set(", ".join(ids))
        run_search()

    def _on_aid_filter(*_a: Any) -> None:
        aid_filter_cb["values"] = _suggest_aids(aid_filter_var.get())
        load_aid_list()

    def _on_aid_filter_pick(_e: Any = None) -> None:
        sel_txt = aid_filter_cb.get().strip()
        if not sel_txt:
            return
        name = sel_txt.rsplit("　(", 1)[0]
        sel_prev = _aid_selected_ids()
        aid_box.selection_clear(0, "end")
        for i in range(aid_box.size()):
            if aid_box.get(i).rsplit("　(", 1)[0] == name:
                aid_box.selection_set(i)
                break
        _on_aid_select()

    def _suggest_aids(prefix: str) -> List[str]:
        p = prefix.strip().lower()
        if not p:
            return [f"{name}　({n})" for aid, name, n in
                    author_histogram(L["recs"])[:8]]
        cands = [(aid, name, n) for aid, name, n in author_histogram(L["recs"])
                 if p in str(name).lower() or p in aid]
        return [f"{name}　({n})" for aid, name, n in cands[:20]]

    def edit_dtags_dialog() -> None:
        """编辑选中作品的衍生标签：原TAG只读展示，衍生TAG可增删、可一键导入原TAG。"""
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在结果里选中一行（一行=一个作品的某一页）")
            return
        vals = tree.item(sel[0], "values")
        # ID 列可能带「×N页」多页标记，先还原成纯 ID
        work_id = str(vals[0]).split("×", 1)[0]
        rec = next((r for r in L["recs"] if str(r.get("id")) == work_id), None)
        if rec is None:
            messagebox.showinfo("提示", f"找不到作品 {work_id} 的记录")
            return

        dlg = tk.Toplevel(root)
        dlg.title(f"编辑衍生标签 —— 作品 {work_id}")
        dlg.transient(root)
        dlg.grab_set()
        ttk.Label(dlg, text=f"标题：{rec.get('title')}", font=("Microsoft YaHei UI", 10, "bold")
                  ).pack(anchor="w", padx=12, pady=(12, 2))

        # 原TAG：只读 + 提示不修改
        ttk.Label(dlg, text="原有TAG（来自 pixiv，不建议修改）",
                  foreground="#888").pack(anchor="w", padx=12, pady=(6, 2))
        orig_box = tk.Listbox(dlg, width=64, height=4,
                              font=("Microsoft YaHei UI", 9), exportselection=False,
                              fg="#666", selectbackground="#e6e6e6")
        orig_box.pack(padx=12, fill="x")
        for t in (rec.get("tags") or []):
            orig_box.insert("end", f"・ {t}")

        # 衍生TAG：可编辑
        ttk.Label(dlg, text="衍生TAG（你自己的标签，可增删）",
                  foreground="#06c").pack(anchor="w", padx=12, pady=(8, 2))
        dlg_tag_list = tk.Listbox(dlg, width=64, height=6,
                                  font=("Microsoft YaHei UI", 9), exportselection=False)
        dlg_tag_list.pack(padx=12, fill="x")

        def refresh_list() -> None:
            dlg_tag_list.delete(0, "end")
            for t in work_dtags(L["lib"].index_dir, work_id):
                dlg_tag_list.insert("end", t)

        refresh_list()

        new_tag_var = tk.StringVar()
        entry_row = ttk.Frame(dlg)
        entry_row.pack(fill="x", padx=12, pady=8)
        ent = ttk.Entry(entry_row, textvariable=new_tag_var, width=30)
        ent.pack(side="left")

        def add_tag() -> None:
            s = new_tag_var.get().strip()
            if not s:
                return
            add_work_dtags(L["lib"].index_dir, work_id, [s])
            new_tag_var.set("")
            refresh_list()

        ent.bind("<Return>", lambda _e: add_tag())
        ttk.Button(entry_row, text="添加", command=add_tag).pack(side="left", padx=4)
        ttk.Button(entry_row, text="删除选中",
                   command=lambda: (
                       set_work_dtags(L["lib"].index_dir, work_id,
                                      [dlg_tag_list.get(i) for i in range(dlg_tag_list.size())
                                       if i not in dlg_tag_list.curselection()]),
                       refresh_list())).pack(side="left", padx=4)
        ttk.Button(entry_row, text="一键导入原TAG",
                   command=lambda: (
                       add_work_dtags(L["lib"].index_dir, work_id,
                                      [str(t) for t in (rec.get("tags") or [])]),
                       refresh_list())).pack(side="left", padx=4)

        def apply_close() -> None:
            dlg.destroy()
            refresh_list()          # 刷新整个图库页面的衍生标签列表
            load_dtags_now()
            run_search()

        def load_dtags_now() -> None:
            L["recs"] = L["lib"].load_records()
            load_dtag_list()
            load_aid_list()

        ttk.Button(dlg, text="完成", command=apply_close).pack(anchor="e", padx=12, pady=8)

    def run_search(*_a: Any) -> None:
        recs = L["recs"]
        hits = search_records_advanced(
            recs,
            text=q_text_var.get(),
            # 列表项形如 "标签　(12)"，取前面的真实标签名
            tags=[t.rsplit("　(", 1)[0] for t in selected_tags()],
            tags_match_all=bool(tag_all_var.get()),
            d_tags=[t.rsplit("　(", 1)[0] for t in selected_dtags()],
            d_tags_match_all=bool(dtag_all_var.get()),
            author=q_author_var.get(),
            author_ids=[w for w in re.split(r"[\s,，]+", q_aid_var.get().strip()) if w],
            date_from=q_date_range["from_value"](),
            date_to=q_date_range["to_value"](),
            min_likes=int(q_likes_var.get() or 0),
            min_bookmarks=int(q_bm_var.get() or 0),
            r18_mode=q_r18_var.get(),
            ai_mode=q_ai_var.get(),
            limit=int(q_limit_var.get() or 0),
            sort_by=q_sort_var.get(),
        )
        tree.delete(*tree.get_children())
        # 多页标记：同一作品在当前结果里出现几页，就在 ID 列标注「×N页」，避免看起来像重复记录
        page_counts: Dict[str, int] = {}
        for r in hits:
            iid = str(r.get("id") or "")
            page_counts[iid] = page_counts.get(iid, 0) + 1
        for r in hits:
            dt = "、".join(str(t) for t in (r.get("d_tags") or [])[:3]) or "—"
            iid = str(r.get("id") or "")
            id_label = f"{iid}×{page_counts[iid]}页" if page_counts.get(iid, 0) > 1 else iid
            tree.insert("", "end", values=(
                id_label, r.get("title"), r.get("author"),
                "、".join(str(t) for t in (r.get("tags") or [])[:6]), dt,
                as_int(r.get("like_count"), 0), as_int(r.get("bookmark_count"), 0),
                str(r.get("create_date") or "")[:10],
                format_size(as_int(r.get("bytes"), 0)), r.get("file")))
        base = f"命中 {len(hits)} 条 / 图库共 {len(recs)} 条"
        # 分级隐藏提示：当前分级过滤掉了多少条（用户看不到的）
        if q_r18_var.get() != "all":
            hits_all = search_records_advanced(
                recs, r18_mode="all",
                text=q_text_var.get() or "",
                author_ids=[w for w in re.split(r"[\s,，]+", q_aid_var.get().strip()) if w],
                d_tags=[t.rsplit("　(", 1)[0] for t in selected_dtags()],
                tags=[t.rsplit("　(", 1)[0] for t in selected_tags()],
            )
            hidden = len(hits_all) - len(hits)
            if hidden > 0:
                base += f"　｜　另有 {hidden} 条被分级隐藏（当前只看全年龄，改「全年龄+R-18」可见）"
        lib_status_var.set(base)

    def reset_search() -> None:
        q_text_var.set("")
        q_author_var.set("")
        q_aid_var.set("")
        q_date_range["set_values"]("", "")
        q_likes_var.set(0)
        q_bm_var.set(0)
        q_r18_var.set("hide")
        q_ai_var.set("all")
        q_sort_var.set("default")
        tag_box.selection_clear(0, "end")
        dtag_box.selection_clear(0, "end")
        run_search()

    def _entry_path() -> Optional[Path]:
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在结果里选中一行")
            return None
        rel = tree.item(sel[0], "values")[9]   # 列顺序：id,title,author,tags,dtags,likes,bm,date,size,file
        return L["lib"].root / str(rel).replace("/", os.sep)

    def open_selected(*_a: Any) -> None:
        p = _entry_path()
        if p is None:
            return
        if not p.exists():
            messagebox.showerror("文件不存在", str(p))
            return
        open_path(p)

    def open_selected_folder(*_a: Any) -> None:
        p = _entry_path()
        if p is None:
            return
        folder = p.parent if p.exists() else L["lib"].root
        open_path(folder)

    def export_hits() -> None:
        rows = [tree.item(i, "values") for i in tree.get_children()]
        if not rows:
            messagebox.showinfo("提示", "当前没有结果可导出")
            return
        p = filedialog.asksaveasfilename(defaultextension=".csv",
                                         initialfile="搜索结果.csv",
                                         filetypes=[("CSV 文件", "*.csv")])
        if not p:
            return
        with open(native_path(p), "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["作品ID", "标题", "画师", "标签", "点赞", "收藏", "发布时间", "大小", "文件"])
            w.writerows(rows)
        lib_status_var.set(f"已导出 {len(rows)} 条到 {p}")

    lb = ttk.Frame(tab_lib, padding=(0, 0, 0, 0))
    for txt, fn in (("检索", run_search), ("重置", reset_search), ("打开图片", open_selected),
                    ("打开所在文件夹", open_selected_folder), ("导出 CSV", export_hits)):
        ttk.Button(brow, text=txt, command=fn).pack(side="right", padx=2)
    for w in (qe, q_aid_entry):
        w.bind("<Return>", run_search)
    def _suggest_tags(prefix: str, hist: List[Tuple[str, int]]) -> List[str]:
        """给输入框的自动补全候选：匹配前缀/包含的已存在标签，按出现次数排序。"""
        p = prefix.strip().lower()
        if not p:
            return []
        cands = [t for t, _n in hist if p in t.lower()]
        cands.sort(key=lambda t: (-next((n for tt, n in hist if tt == t), 0), t))
        return cands[:20]

    def _on_tag_filter(*_a: Any) -> None:
        # 输入框内容变化：过滤左侧列表 + 更新下拉候选
        tag_filter_cb["values"] = _suggest_tags(tag_filter_var.get(),
                                                tag_histogram(L["recs"]))
        load_tags()

    def _on_tag_filter_pick(_e: Any = None) -> None:
        # 从下拉候选里选了一个：点选该标签到 tag_box 并检索
        sel = tag_filter_cb.get().strip()
        if not sel:
            return
        sel_name = sel.rsplit("　(", 1)[0]
        found = -1
        for i in range(tag_box.size()):
            if tag_box.get(i).rsplit("　(", 1)[0] == sel_name:
                found = i
                break
        if found >= 0:
            tag_box.selection_clear(0, "end")
            tag_box.selection_set(found)
            tag_box.see(found)
        run_search()

    def _on_dtag_filter(*_a: Any) -> None:
        dtag_filter_cb["values"] = _suggest_tags(dtag_filter_var.get(),
                                                 dtag_histogram(L["recs"]))
        load_dtag_list()
        load_aid_list()

    def _on_dtag_filter_pick(_e: Any = None) -> None:
        sel = dtag_filter_cb.get().strip()
        if not sel:
            return
        sel_name = sel.rsplit("　(", 1)[0]
        for i in range(dtag_box.size()):
            if dtag_box.get(i).rsplit("　(", 1)[0] == sel_name:
                dtag_box.selection_clear(0, "end")
                dtag_box.selection_set(i)
                dtag_box.see(i)
                break
        run_search()

    tag_filter_var.trace_add("write", _on_tag_filter)
    dtag_filter_var.trace_add("write", _on_dtag_filter)
    aid_filter_var.trace_add("write", _on_aid_filter)
    tag_filter_cb.bind("<<ComboboxSelected>>", _on_tag_filter_pick)
    dtag_filter_cb.bind("<<ComboboxSelected>>", _on_dtag_filter_pick)
    aid_filter_cb.bind("<<ComboboxSelected>>", _on_aid_filter_pick)
    # 单击标签即检索（多选时每次释放也刷新）；双击保留
    tag_box.bind("<ButtonRelease-1>", lambda _e: run_search())
    dtag_box.bind("<ButtonRelease-1>", lambda _e: run_search())
    aid_box.bind("<ButtonRelease-1>", _on_aid_select)
    tag_box.bind("<Double-1>", run_search)
    dtag_box.bind("<Double-1>", run_search)
    aid_box.bind("<Double-1>", _on_aid_select)

    # ==================================================================================
    # 标签页 3：图库位置
    # ==================================================================================
    tab_loc = ttk.Frame(nb, padding=12)
    nb.add(tab_loc, text="　图库位置　")

    ttk.Label(tab_loc, text="图库在电脑中的位置", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
    ttk.Label(tab_loc, foreground="#666",
              text="图库 = 下载的原图 + 分类目录 + 索引。改到别处后，检索与后续下载都会用新位置。").pack(
        anchor="w", pady=(2, 10))

    loc_var = tk.StringVar(value=str(L["lib"].root))
    loc_row = ttk.Frame(tab_loc)
    loc_row.pack(fill="x")
    loc_entry = ttk.Entry(loc_row, textvariable=loc_var, width=76)
    loc_entry.pack(side="left")

    loc_info_var = tk.StringVar(value="")
    loc_info = ttk.Label(tab_loc, textvariable=loc_info_var, foreground="#666", justify="left")
    loc_info.pack(anchor="w", pady=(10, 0))

    def refresh_lib_info() -> None:
        lib_path_var.set(f"图库位置：{L['lib'].root}")
        try:
            recs = L["lib"].load_records()
        except Exception as exc:  # noqa: BLE001
            recs = []
            loc_info_var.set(f"读取索引失败：{exc}")
            return
        L["recs"] = recs
        works = len({str(r.get("id")) for r in recs})
        total = sum(as_int(r.get("bytes"), 0) for r in recs)
        # 记录 vs 作品 的解释：记录=一张图，作品=一个 pixiv 作品（多页漫画算多张图）
        multi_note = ""
        if recs:
            per = len(recs) / max(1, works)
            if per != 1.0:
                from collections import Counter
                counts = Counter(str(r.get("id")) for r in recs)
                multi = sum(1 for c in counts.values() if c > 1)
                multi_note = (f"\n记录按「图」计、作品按「pixiv 作品」计：平均每个作品 {per:.1f} 页，"
                              f"其中 {multi} 个多页作品")
        try:
            free = format_size(shutil.disk_usage(native_path(L["lib"].root)).free)
        except OSError:
            free = "未知"
        exists = L["lib"].root.is_dir()
        loc_info_var.set(
            f"目录存在：{'是' if exists else '否（改成新位置后会自动创建）'}\n"
            f"记录 {len(recs)} 条 / 作品 {works} 个 / 原图 {format_size(total)}\n"
            f"所在磁盘剩余空间：{free}\n"
            f"索引文件：{L['lib'].md_path if os.path.isfile(native_path(L['lib'].md_path)) else '（尚未生成）'}"
            + multi_note)
        lib_count_var.set(f"记录：{len(recs)}")
        load_tags()
        load_dtag_list()
        load_aid_list()
        run_search()

    def browse_library() -> None:
        p = filedialog.askdirectory(title="选择图库位置", initialdir=str(L["lib"].root))
        if p:
            loc_var.set(os.path.normpath(p))

    def apply_library_location() -> None:
        raw = loc_var.get().strip()
        if not raw:
            messagebox.showinfo("提示", "请填写图库位置")
            return
        new = Path(raw).expanduser()
        if not new.is_absolute():
            new = (program_dir / new).resolve()
        old = L["lib"].root
        if str(new) == str(old):
            messagebox.showinfo("提示", "位置没有变化")
            return
        try:
            new.mkdir(parents=True, exist_ok=True)
            probe = new / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            messagebox.showerror("无法使用该位置", f"{new}\n{exc}")
            return
        # 写回 config.json
        try:
            data = read_json(cfg_path, {}) or {}
            if not isinstance(data, dict):
                data = {}
            data["output_dir"] = str(new)
            atomic_write_json(cfg_path, data)
        except OSError as exc:
            messagebox.showerror("写入配置失败", f"{cfg_path}\n{exc}")
            return
        L["cfg"]["output_dir"] = str(new)
        L["lib"] = Library(new)
        L["lib"].ensure()
        root.title(f"pixiv 爬虫 v{__version__}　图库：{new}")
        loc_var.set(str(new))
        refresh_lib_info()
        load_tags()
        load_dtag_list()
        load_aid_list()
        run_search()
        messagebox.showinfo("已切换图库位置",
                            f"新位置：{new}\n配置已保存到：{cfg_path}\n\n"
                            f"注意：旧的图库文件仍留在 {old}，需要的话请自行移动。\n"
                            "如果新位置已有图库，直接就能检索；若为空，用爬取页下载即可。")

    def rebuild_index() -> None:
        try:
            info = L["lib"].rebuild_all()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("重建失败", str(exc))
            return
        refresh_lib_info()
        messagebox.showinfo("索引已重建", f"共 {info['records']} 条\n{info['md']}\n{info['csv']}")

    loc_btns = ttk.Frame(tab_loc)
    loc_btns.pack(fill="x", pady=(10, 0))
    ttk.Button(loc_btns, text="浏览…", command=browse_library).pack(side="left")
    ttk.Button(loc_btns, text="应用并切换", command=apply_library_location).pack(side="left", padx=6)
    ttk.Button(loc_btns, text="打开图库目录",
               command=lambda: open_path(L["lib"].root)).pack(side="left", padx=6)
    ttk.Button(loc_btns, text="打开索引目录",
               command=lambda: open_path(L["lib"].index_dir)).pack(side="left", padx=6)
    ttk.Button(loc_btns, text="重建索引", command=rebuild_index).pack(side="left", padx=6)

    ttk.Label(tab_loc, text="　").pack(anchor="w", pady=8)
    ttk.Label(tab_loc, text="分类子目录（点击直接打开）",
              font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
    sub_btns = ttk.Frame(tab_loc)
    sub_btns.pack(fill="x", pady=(4, 0))
    for name in ("by_tag", "by_author", "by_keyword", "_originals", "_meta"):
        ttk.Button(sub_btns, text=name,
                   command=lambda n=name: open_path(L["lib"].root / n)).pack(side="left", padx=(0, 6))

    # ==================================================================================
    # 标签页 4：读取能力（登录体检）
    # ==================================================================================
    tab_cap = ttk.Frame(nb, padding=10)
    nb.add(tab_cap, text="　读取能力　")

    ttk.Label(tab_cap, text="读取能力体检", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
    ttk.Label(tab_cap, foreground="#666", justify="left",
              text="同样一个关键词，登录后能读到的作品数是匿名的 10 倍以上。\n"
                   "这一页会实际发请求测出你当前到底能读多少，并给出对应的修复步骤。").pack(
        anchor="w", pady=(2, 8))

    cap_banner = tk.StringVar(value="尚未检测　—— 点下面的「开始检测」")
    cap_banner_lbl = ttk.Label(tab_cap, textvariable=cap_banner,
                               font=("Microsoft YaHei UI", 11, "bold"), foreground="#666")
    cap_banner_lbl.pack(anchor="w")

    cap_sub = tk.StringVar(value="")
    ttk.Label(tab_cap, textvariable=cap_sub, foreground="#333", justify="left").pack(
        anchor="w", pady=(2, 8))

    cap_box = ttk.LabelFrame(tab_cap, text="探测项目", padding=8)
    cap_box.pack(fill="x")
    cap_rows: Dict[str, ttk.Label] = {}
    cap_texts: Dict[str, tk.StringVar] = {}
    for key, title in (("cred", "① 登录凭据"), ("login", "② 登录态"),
                       ("capacity", "③ 可读取量（登录 vs 匿名）"), ("download", "④ 原图下载")):
        row = ttk.Frame(cap_box)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text=title, width=24, anchor="w").pack(side="left")
        v = tk.StringVar(value="—")
        lbl = ttk.Label(row, textvariable=v, foreground="#666", justify="left")
        lbl.pack(side="left")
        cap_rows[key] = lbl
        cap_texts[key] = v

    cap_advice = ttk.LabelFrame(tab_cap, text="下一步建议", padding=8)
    cap_advice.pack(fill="x", pady=(8, 0))
    cap_advice_var = tk.StringVar(value="（检测后显示）")
    ttk.Label(cap_advice, textvariable=cap_advice_var, justify="left",
              foreground="#333").pack(anchor="w")

    cap_btns = ttk.Frame(tab_cap, padding=(0, 10, 0, 0))
    cap_btns.pack(fill="x")
    cap_probe_btn = ttk.Button(cap_btns, text="开始检测")
    cap_probe_btn.pack(side="left")
    ttk.Label(cap_btns, text="深度：").pack(side="left", padx=(10, 0))
    # 默认 20 页：匿名约 600 个会在第 11 页左右到顶，而登录要到 20 页以上才显出差，
    # 太浅（如 4 页）会让两边同时触顶、结论变成"测不出"（实测踩到）
    cap_depth_var = tk.IntVar(value=20)
    ttk.Spinbox(cap_btns, from_=2, to=110, textvariable=cap_depth_var, width=5).pack(side="left")
    ttk.Label(cap_btns, text=" 页（越大越准，也越慢）", foreground="#777").pack(side="left")
    cap_anon_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(cap_btns, text="跳过匿名对比（更快）", variable=cap_anon_var).pack(
        side="left", padx=(10, 0))

    cap_fix = ttk.LabelFrame(tab_cap, text="如果检测不通过，用这些按钮修复", padding=8)
    cap_fix.pack(fill="x", pady=(10, 0))

    cap_status_var = tk.StringVar(value="就绪")
    fix_row1 = ttk.Frame(cap_fix)
    fix_row1.pack(fill="x")
    ttk.Button(fix_row1, text="从浏览器自动导入 Cookie",
               command=lambda: import_cookie_from_browser()).pack(side="left")
    ttk.Button(fix_row1, text="手动填 PHPSESSID…",
               command=lambda: manual_cookie_dialog()).pack(side="left", padx=6)
    ttk.Button(fix_row1, text="用 pixiv 官网登录（OAuth）",
               command=lambda: start_oauth_dialog()).pack(side="left", padx=6)
    ttk.Button(fix_row1, text="打开凭据库目录",
               command=lambda: open_path(CRED_DIR)).pack(side="left", padx=6)
    ttk.Button(fix_row1, text="清除已保存凭据",
               command=lambda: forget_credentials()).pack(side="left", padx=6)
    ttk.Label(cap_fix, textvariable=cap_status_var, foreground="#666",
              justify="left").pack(anchor="w", pady=(6, 0))

    ttk.Label(cap_fix, text="凭据优先级（下面的覆盖上面的）", font=("Microsoft YaHei UI", 9, "bold")
              ).pack(anchor="w", pady=(8, 2))
    prio = tk.Text(cap_fix, height=6, wrap="none", font=("Consolas", 9),
                   background="#f7f7f7", relief="flat")
    prio.pack(fill="x")
    prio.insert("1.0",
                "1. 命令行参数        --cookie / --refresh-token\n"
                f"2. 环境变量          PIXIV_PHPSESSID / PIXIV_REFRESH_TOKEN\n"
                f"3. 凭据库（推荐）    {CRED_FILE}\n"
                f"4. config.json       {cfg_path}  的 cookie_phpsessid / refresh_token\n")
    prio.configure(state="disabled")

    def render_capability(res: Dict[str, Any]) -> None:
        """把 probe_capability 的结果渲染到界面。"""
        verdict = res.get("verdict")
        style = {
            "full": ("✓ 完全可用　登录权限已生效，可以全量爬取", "#0a0"),
            "limited": ("△ 部分受限　能爬，但下面有项目没通过", "#c80"),
            "anonymous": ("✗ 仅匿名　只能读到约 600 个/排序（登录后可提升 10 倍）", "#c00"),
            "error": ("✗ 检测失败", "#c00"),
        }.get(verdict, ("? 未知结论", "#666"))
        cap_banner.set(style[0])
        cap_banner_lbl.configure(foreground=style[1])

        cred = res.get("cred") or {}
        login = res.get("login") or {}
        capd = res.get("capacity") or {}
        dl = res.get("download") or {}

        bits = []
        if login:
            bits.append(f"{login.get('name') or '（未知昵称）'}"
                        f"（@{login.get('pixiv_id') or login.get('user_id') or '?'}）")
            bits.append("已开启成人内容" if login.get("adult") else "未开启成人内容")
        else:
            bits.append("未登录")
        bits.append(f"凭据来源：{cred.get('source') or '无'}")
        cap_sub.set("　·　".join(bits))

        def setrow(key: str, ok: Optional[bool], text: str) -> None:
            mark = "✓ " if ok is True else ("✗ " if ok is False else "· ")
            cap_texts[key].set(mark + text)
            cap_rows[key].configure(
                foreground="#0a0" if ok is True else ("#c00" if ok is False else "#666"))

        setrow("cred", bool(cred.get("has_cookie") or cred.get("has_token")),
               f"{cred.get('source') or '无凭据'}"
               + (f"　cookie {cred.get('cookie_len')} 字符" if cred.get("has_cookie") else "")
               + ("　refresh_token 已配置" if cred.get("has_token") else ""))

        if login:
            setrow("login", True, f"有效：{login.get('name')}（id={login.get('user_id') or '?'}）")
        elif res.get("verdict") == "error":
            setrow("login", None, res.get("error") or "未完成")
        else:
            setrow("login", False, "无效或未登录 → 实际按匿名处理")

        if capd:
            inc = bool(capd.get("inconclusive"))
            ratio = capd.get("ratio") or 0
            ok_cap = (capd.get("login") or 0) > 900
            setrow("capacity", None if inc else ok_cap,
                   f"登录可读 {capd.get('login')} 个 ／ 匿名 {capd.get('anon')} 个"
                   f"（{ratio} 倍）"
                   + (f"　pixiv 报告总数 {capd.get('total_login')}" if capd.get("total_login") else "")
                   + ("　※两边都到探测上限，建议加大深度重测" if inc else ""))

        if dl:
            if dl.get("ok"):
                setrow("download", True,
                       f"作品 {dl.get('id')} 原图可下载（{format_size(as_int(dl.get('bytes'), 0))}，"
                       f"{dl.get('ctype') or '图片'}）")
            else:
                setrow("download", False, dl.get("note") or "失败")

        advice = res.get("advice") or []
        cap_advice_var.set("\n".join(f"{i}. {a}" for i, a in enumerate(advice, 1))
                           if advice else "（无）")
    def start_probe() -> None:
        if L.get("probing"):
            return
        L["probing"] = True
        cap_probe_btn.configure(state="disabled")
        cap_banner.set("正在检测…")
        cap_banner_lbl.configure(foreground="#06c")
        for k in cap_texts:
            cap_texts[k].set("检测中…")
        cap_advice_var.set("检测中…")
        depth = max(2, int(cap_depth_var.get() or 8))
        # 主线程读好界面状态再交给后台线程，避免工作线程碰 tkinter 变量
        snap = dict(L["cfg"])
        skip_anon = bool(cap_anon_var.get())
        # 估算耗时给用户一个预期：登录侧 + 匿名侧各 depth 页，按当前限速算
        rps = float(snap.get("requests_per_second") or 1.2)
        est_req = depth * (1 if skip_anon else 2) + 3
        est_sec = est_req / rps if rps > 0 else 0
        started_at = time.time()
        cap_status_var.set(f"检测中：约需 {est_req} 次请求，预计 {est_sec:.0f} 秒"
                          f"（深度 {depth} 页；想更快可把深度调小或勾选「跳过匿名对比」）")

        def on_progress(msg: str) -> None:
            el = time.time() - started_at
            msg_q.put(("__PROBE_TICK__", f"{msg}　已用 {el:.0f} 秒"))

        def worker() -> None:
            try:
                res = probe_capability(snap, max_pages=depth,
                                       check_download=True,
                                       skip_anon=skip_anon,
                                       log=lambda m: msg_q.put(f"[体检] {m}"),
                                       progress=on_progress)
                msg_q.put(("__PROBE_DONE__", res))
            except Exception as exc:  # noqa: BLE001
                msg_q.put(("__PROBE_DONE__", {"verdict": "error",
                                              "error": f"{type(exc).__name__}: {exc}",
                                              "checks": [], "advice": []}))
        threading.Thread(target=worker, daemon=True).start()

    cap_probe_btn.configure(command=start_probe)

    def import_cookie_from_browser() -> None:
        """从本机浏览器读 cookie 并存入凭据库；失败时打印原因与手动方案。"""
        cap_status_var.set("正在读取浏览器 cookie…")
        root.update_idletasks()

        def worker() -> None:
            try:
                sess = cookie_from_browser(verbose=False)
            except Exception as exc:  # noqa: BLE001
                sess = None
                msg_q.put(f"[体检] 读取浏览器失败：{type(exc).__name__}: {exc}")
            if not sess:
                msg_q.put(("__COOKIE_FAIL__", None))
                return
            save_credential_store(sess, kind="phpsessid")
            L["cfg"]["cookie_phpsessid"] = sess
            msg_q.put(("__COOKIE_OK__", len(sess)))
        threading.Thread(target=worker, daemon=True).start()

    def start_oauth_dialog() -> None:
        """OAuth 登录：账号密码只进 pixiv 官方页面，本程序只拿到可吊销的 refresh_token。

        这是除 cookie 之外的第二条修复路径，比存账号密码安全得多：
        refresh_token 可单独吊销、长期有效、走官方 App API（网页接口改版时也能兜底）。
        """
        verifier, challenge = pkce_pair()
        auth_url = build_authorize_url(challenge)

        dlg = tk.Toplevel(root)
        dlg.title("用 pixiv 官网登录（OAuth）")
        dlg.transient(root)
        dlg.grab_set()
        ttk.Label(dlg, text="OAuth 登录（推荐，第二条修复路径）",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        ttk.Label(dlg, text="账号密码只在 pixiv 官方页面输入，本程序不接触、也不保存它们。\n"
                            "完成后会得到 refresh_token —— 可随时吊销、长期有效、走官方 App API。",
                  justify="left", foreground="#333").pack(anchor="w", padx=12)
        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=12, pady=8)
        ttk.Button(row, text="1. 打开授权页面",
                   command=lambda: open_path(auth_url)).pack(side="left")
        ttk.Label(row, text="会在浏览器打开 pixiv 官方登录页",
                  foreground="#777").pack(side="left", padx=8)
        ttk.Label(dlg, text="2. 登录后浏览器会跳到回调页，把地址栏整段复制过来粘贴：",
                  justify="left").pack(anchor="w", padx=12)
        code_var = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=code_var, width=72)
        ent.pack(padx=12, pady=(4, 4), fill="x")
        info = tk.StringVar(value="")
        ttk.Label(dlg, textvariable=info, foreground="#c00").pack(anchor="w", padx=12)

        def complete() -> None:
            callback = code_var.get().strip()
            code = extract_code_from_redirect(callback)
            if not code:
                info.set("没能从粘贴内容里找到授权码：请把浏览器地址栏的整段内容复制过来")
                return
            info.set("正在换取 refresh_token…")
            dlg.update_idletasks()

            def worker() -> None:
                try:
                    data = exchange_code_for_token(code, verifier)
                except CrawlError as exc:
                    msg_q.put(("__OAUTH_FAIL__", str(exc)))
                    return
                token = str(data.get("refresh_token") or "")
                if not token:
                    msg_q.put(("__OAUTH_FAIL__", "pixiv 没有返回 refresh_token，可能授权码已过期，请重试"))
                    return
                try:
                    user = data.get("user") or {}
                    save_credential_store(token, kind="refresh_token",
                                          user={"id": str(user.get("id") or ""),
                                                "name": str(user.get("name") or ""),
                                                "account": str(user.get("account") or "")},
                                          extra={"scope": "app"})
                except OSError as exc:
                    msg_q.put(("__OAUTH_FAIL__", f"写入凭据库失败：{exc}"))
                    return
                L["cfg"]["refresh_token"] = token
                msg_q.put(("__OAUTH_OK__", str(user.get("name") or user.get("account") or "")))
            threading.Thread(target=worker, daemon=True).start()

        brow2 = ttk.Frame(dlg)
        brow2.pack(fill="x", padx=12, pady=8)
        ttk.Button(brow2, text="3. 获取并保存", command=complete).pack(side="left")
        ttk.Button(brow2, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        ent.bind("<Return>", lambda _e: complete())

    def manual_cookie_dialog() -> None:
        """手动粘贴 PHPSESSID —— 附带"怎么找"的说明。"""
        dlg = tk.Toplevel(root)
        dlg.title("手动填写 PHPSESSID")
        dlg.transient(root)
        dlg.grab_set()
        ttk.Label(dlg, text="怎么找到 PHPSESSID", font=("Microsoft YaHei UI", 10, "bold")
                  ).pack(anchor="w", padx=12, pady=(12, 4))
        steps = ("1. 用浏览器打开 https://www.pixiv.net 并确认已登录\n"
                 "2. 按 F12 打开开发者工具 → 顶部选「应用程序 / Application」\n"
                 "3. 左侧 Storage → Cookies → https://www.pixiv.net\n"
                 "4. 找到名为 PHPSESSID 的那一行，复制它的值（41 个字符）\n"
                 "5. 粘贴到下面的输入框，点「保存并检测」")
        ttk.Label(dlg, text=steps, justify="left", foreground="#333").pack(
            anchor="w", padx=12, pady=(0, 8))
        var = tk.StringVar(value="")
        ent = ttk.Entry(dlg, textvariable=var, width=52)
        ent.pack(padx=12, anchor="w")
        ent.focus_set()
        info = tk.StringVar(value="")
        ttk.Label(dlg, textvariable=info, foreground="#c00").pack(anchor="w", padx=12, pady=(4, 0))

        def save() -> None:
            val = var.get().strip()
            if len(val) < 20:
                info.set(f"看起来不完整：只有 {len(val)} 个字符，PHPSESSID 正常是 41 个字符")
                return
            try:
                save_credential_store(val, kind="phpsessid")
            except OSError as exc:
                info.set(f"写入凭据库失败：{exc}")
                return
            L["cfg"]["cookie_phpsessid"] = val
            cap_status_var.set(f"已保存 PHPSESSID（{len(val)} 字符）到 {CRED_FILE}，开始重新检测…")
            dlg.destroy()
            start_probe()

        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=12, pady=10)
        ttk.Button(row, text="保存并检测", command=save).pack(side="left")
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        ent.bind("<Return>", lambda _e: save())

    def forget_credentials() -> None:
        if not messagebox.askyesno("确认", f"删除凭据库？\n{CRED_FILE}\n\n"
                                          "（config.json 里的凭据不受影响）"):
            return
        removed = clear_credential_store()
        # 同时清掉内存里的凭据，否则本次运行仍是登录态
        L["cfg"].pop("cookie_phpsessid", None)
        L["cfg"].pop("refresh_token", None)
        cap_status_var.set("已删除凭据库" if removed else "凭据库本来就不存在")
        start_probe()

    # 打开这一页时自动跑一次体检（延后到事件循环，避免拖慢界面出现）
    # 打开「读取能力」页（tab 4）时自动跑一次体检（延后到事件循环，避免拖慢界面出现）
    nb.bind("<<NotebookTabChanged>>", lambda _e: (
        start_probe() if nb.index("current") == 4 and not L.get("probing") else None))

    # ==================================================================================
    # 标签页 5：爬取历史
    # ==================================================================================
    tab_hist = ttk.Frame(nb, padding=6)
    nb.add(tab_hist, text="　爬取历史　")

    ttk.Label(tab_hist, text="每次爬取的记录：序号（自动标签用）、时间、模式、检索项、结果",
              foreground="#666").pack(anchor="w", pady=(0, 4))

    hcols = ("ord", "started", "mode", "target", "downloaded", "failed", "duration", "auto_tag")
    hheads = ("第N次", "开始时间", "模式", "检索项", "下载", "失败", "耗时", "自动标签")
    hwidths = (60, 150, 80, 300, 60, 60, 60, 220)
    htree = ttk.Treeview(tab_hist, columns=hcols, show="headings", height=14)
    for c, w, h in zip(hcols, hwidths, hheads):
        htree.heading(c, text=h)
        htree.column(c, width=w, anchor="w")
    htree.pack(fill="both", expand=True)
    hvsb = ttk.Scrollbar(tab_hist, orient="vertical", command=htree.yview)
    hvsb.pack(side="right", fill="y")
    htree.configure(yscrollcommand=hvsb.set)

    hist_status_var = tk.StringVar(value="")
    hrow = ttk.Frame(tab_hist, padding=(0, 6, 0, 0))
    hrow.pack(fill="x")
    ttk.Label(hrow, textvariable=hist_status_var, foreground="#666").pack(side="left")
    ttk.Button(hrow, text="刷新", command=lambda: refresh_history_tab()).pack(side="right", padx=2)
    ttk.Button(hrow, text="清除历史…", command=lambda: clear_history()).pack(side="right", padx=2)

    def refresh_history_tab() -> None:
        entries = load_crawl_history(L["lib"].index_dir)
        htree.delete(*htree.get_children())
        for e in entries:
            mode = str(e.get("mode") or "")
            targets = "、".join(str(x) for x in (e.get("keywords") or e.get("artists") or []))
            htree.insert("", "end", values=(
                e.get("ordinal"), str(e.get("started_at") or "")[:19].replace("T", " "),
                mode, targets[:80],
                (e.get("result") or {}).get("downloaded", ""),
                (e.get("result") or {}).get("failed", ""),
                f"{float(e.get('duration_s') or 0):.0f}s", e.get("auto_tag") or ""))
        hist_status_var.set(f"共 {len(entries)} 次爬取")
        # 更新状态栏与衍生标签列表（每次爬取都会新增「第N次爬取」衍生标签）
        load_dtag_list()
        load_aid_list()

    def clear_history() -> None:
        """清空爬取历史。破坏性操作：先弹详细警告，默认不建议。"""
        # 自定义警告对话框：把后果和"不建议"讲清楚，避免手滑
        dlg = tk.Toplevel(root)
        dlg.title("清除爬取历史？")
        dlg.transient(root)
        dlg.grab_set()
        ttk.Label(dlg, text="⚠ 不建议清除爬取历史", font=("Microsoft YaHei UI", 11, "bold"),
                  foreground="#c00").pack(anchor="w", padx=14, pady=(14, 6))
        ttk.Label(dlg, text="清除会带来这些后果：", font=("Microsoft YaHei UI", 9, "bold")
                  ).pack(anchor="w", padx=14, pady=(0, 2))
        ttk.Label(dlg, text=(
            "· 所有爬取记录将被删除，且无法恢复（没有备份）\n"
            "· 「第N次爬取」序号会重置 —— 下次爬取从「第1次爬取」重新开始\n"
            "· 之后新下载的作品将附上重新计数的自动标签，\n"
            "  与旧标签（如「2026年9月29日第12次爬取」）脱节，溯源会错乱\n"
            "· 图库里的作品和衍生标签不受影响"),
                  justify="left", foreground="#333").pack(anchor="w", padx=14)
        ttk.Label(dlg, text=(
            "正常使用一般不需要清除：可以在「爬取历史」页查看每次的记录，\n"
            "历史文件很小，留着也不占空间。请确认你真的要清。"),
                  justify="left", foreground="#a00").pack(anchor="w", padx=14, pady=(6, 0))
        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=14, pady=12)
        ttk.Button(row, text="仍然清除", command=lambda: do_clear(dlg)).pack(side="right")
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="right", padx=(0, 6))

        def do_clear(box: Any) -> None:
            box.destroy()
            p = history_path(L["lib"].index_dir)
            if os.path.exists(native_path(p)):
                try:
                    os.remove(native_path(p))
                except OSError as exc:
                    messagebox.showerror("清除失败", str(exc))
                    return
            refresh_history_tab()

    reload_history_tab = refresh_history_tab   # 供爬取完成回调复用

    # ==================================================================================
    # 爬取执行（后台线程 + 队列 + 主线程渲染）
    # ==================================================================================
    def refresh_list(*_: Any) -> None:
        L["recs"] = L["lib"].load_records()
        lib_count_var.set(f"记录：{len(L['recs'])}")

    def gui_cfg_snapshot() -> Dict[str, Any]:
        """把界面上所有选择收成一个配置字典（爬取与预估共用，避免两处不一致）。"""
        sub_cfg = dict(L["cfg"])
        # 分级：由三个勾选框决定，具体映射交给 r18_plan（服务器 mode + 本地精筛）
        lvls = picked_levels()
        server_mode, allowed = r18_plan(lvls)
        sub_cfg["mode"] = server_mode
        sub_cfg["r18_levels"] = tuple(allowed)
        # 动图：格式选 none 就是不收动图；选了格式就收并转码
        sub_cfg["ugoira_format"] = ugo_fmt_var.get()
        sub_cfg["skip_ugoira"] = ugo_fmt_var.get() == "none"
        sub_cfg["ugoira_keep_zip"] = bool(ugo_keep_var.get())
        sub_cfg["ai_mode"] = str(ai_mode_var.get() or "all")
        sub_cfg["artist_ids"] = [w for w in re.split(r"[\s,，]+", crawl_aid_var.get().strip()) if w]
        sub_cfg["date_from"] = date_range["from_value"]()
        sub_cfg["date_to"] = date_range["to_value"]()
        sub_cfg["min_likes"] = int(min_likes_var.get() or 0)
        sub_cfg["min_bookmarks"] = int(min_bm_var.get() or 0)
        sub_cfg["max_pages_per_work"] = int(mpw_var.get() or 0)
        return sub_cfg

    def start_crawl() -> None:
        if L["busy"]:
            messagebox.showinfo("提示", "正在爬取中，请等待当前任务结束")
            return
        kws = [w for w in re.split(r"[\s,，]+", kw_var.get().strip()) if w]
        if not kws:
            messagebox.showinfo("提示", "请先输入关键词")
            return
        if not picked_levels():
            messagebox.showinfo("提示", "「内容」里至少要勾一个（全年龄 / R-18 / R-18G），"
                                        "否则没有任何内容会被爬取。")
            return
        if date_error[0]:
            if not messagebox.askyesno("时间范围有问题",
                                       "起始时间不晚于结束时间才有效，当前设置不会做时间筛选。\n仍要继续吗？"):
                return
        L["busy"] = True
        log.delete("1.0", "end")
        status_var.set("正在爬取…")
        batch_dtags_btn.configure(state="disabled")   # 新一批开始时清掉"上一批可标记"
        L["last_crawl_works"] = []
        crawl_prog.configure(value=0)
        crawl_prog_var.set("准备中…")
        L["stop_evt"] = threading.Event()
        sub_cfg = gui_cfg_snapshot()
        pages, order = int(pages_var.get()), str(order_var.get())
        use_deep, dry = bool(deep_var.get()), bool(dry_var.get())
        r18_levels = sub_cfg.get("r18_levels")
        ai_mode = str(sub_cfg.get("ai_mode") or "all")
        artist_ids = sub_cfg.get("artist_ids") or []
        # 检索范围 / 收藏夹
        crawl_scope = scope_var.get()
        bm_uid = bm_uid_var.get().strip()
        if crawl_scope == "bookmarks" and not bm_uid:
            messagebox.showinfo("提示", "收藏夹模式需要填「收藏夹：」用户ID")
            L["busy"] = False
            return

        def on_progress(kw: Dict[str, Any]) -> None:
            msg_q.put(("__PROG__", kw))

        def worker() -> None:
            try:
                run_crawl(sub_cfg, L["lib"], kws, pages=pages, order=order,
                          mode=str(sub_cfg.get("mode") or "all"),
                          s_mode=str(sub_cfg.get("s_mode") or "s_tag"),
                          max_works=int(sub_cfg.get("max_works_per_run") or 0),
                          deep=use_deep, dry_run=dry,
                          r18_levels=r18_levels, ai_mode=ai_mode,
                          artist_ids=artist_ids,
                          scope=crawl_scope,
                          bookmark_uids=[bm_uid] if crawl_scope == "bookmarks" else None,
                          bookmark_rest="hide" if bm_hide_var.get() else "show",
                          bookmark_tag=bm_tag_var.get().strip(),
                          stop_event=L["stop_evt"], progress_cb=on_progress,
                          log=lambda m: msg_q.put(m))
            except Exception as exc:  # noqa: BLE001
                msg_q.put(f"[error] {type(exc).__name__}: {exc}")
            finally:
                msg_q.put(DONE)

        threading.Thread(target=worker, daemon=True).start()

    def stop_crawl() -> None:
        """停止当前爬取：置位停止事件，工作线程在下一个安全点优雅退出，
        已下载的图和索引都会保存，下次点「开始爬取」会自动续跑（已下载的跳过）。"""
        evt = L.get("stop_evt")
        if not L.get("busy") or evt is None:
            status_var.set("当前没有运行中的爬取")
            return
        evt.set()
        status_var.set("已请求停止，正在保存…（已下载内容不会丢）")
        crawl_prog_var.set("正在停止…")

    def mark_batch_dtags() -> None:
        """给「最近一次爬取」的这批新作品批量添加衍生标签。

        爬取结束时 DONE 处理器会把本次 new_works 存进 L["last_crawl_works"]；
        这里弹窗让用户输入一个或多个衍生标签，然后批量写入 dtags.json。
        """
        works = L.get("last_crawl_works") or []
        if not works:
            messagebox.showinfo("提示", "还没有可标记的爬取结果。\n"
                                        "请先完成一次爬取（或追更同步），结束后就能给"
                                        "这批新下载的图批量加标签。")
            return
        dlg = tk.Toplevel(root)
        dlg.title(f"给本次爬取的 {len(works)} 个作品加衍生标签")
        dlg.transient(root)
        dlg.grab_set()
        ttk.Label(dlg, text=f"本次爬取新下载了 {len(works)} 个作品，将给这**一批**统一加衍生标签：",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=14, pady=(14, 2))
        ttk.Label(dlg, text="标签可填多个，用逗号 / 空格 / 顿号分隔（例如：收藏, 壁纸 2026）：",
                  foreground="#555").pack(anchor="w", padx=14)
        var = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=var, width=52)
        ent.pack(padx=14, fill="x", pady=8)
        info = tk.StringVar(value="")
        ttk.Label(dlg, textvariable=info, foreground="#c00").pack(anchor="w", padx=14)

        def ok() -> None:
            raw = var.get()
            tags = [s.strip() for s in re.split(r"[,，、\s]+", raw) if s.strip()]
            if not tags:
                info.set("请至少填一个标签")
                return
            idx = L["lib"].index_dir
            for wid in works:
                add_work_dtags(idx, wid, tags)
            dlg.destroy()
            # 刷新图库（列表、衍生标签、检索）
            refresh_list()
            load_tags()
            load_dtag_list()
            run_search()
            logln(f"[i] 已给本次爬取的 {len(works)} 个作品批量添加衍生标签：{'、'.join(tags)}")
            status_var.set(f"已标记 {len(works)} 个作品")

        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=14, pady=(4, 12))
        ttk.Button(row, text="确定", command=ok).pack(side="left")
        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        ent.bind("<Return>", lambda _e: ok())
        ent.focus_set()

    def _render_crawl_progress(kw: Dict[str, Any]) -> None:
        """把后台上报的进度渲染到爬取页进度条。kw 来自 CrawlSession.report。"""
        stage = kw.get("stage")
        done = int(kw.get("done") or 0)
        total = int(kw.get("total") or 1)
        detail = str(kw.get("detail") or "")
        if total > 0:
            crawl_prog.configure(maximum=total, value=done)
        pct = int(done * 100 / total) if total else 0
        stage_names = {"combo": "搜索组合", "artist": "作品", "artist_scan": "画师",
                       "artist_dl": "作品", "follow": "画师", "walk": "时间段"}
        label = stage_names.get(stage, stage)
        crawl_prog_var.set(f"{label}{done}/{total}（{pct}%）　{detail[:36]}")

    def start_estimate() -> None:
        """预估：先算量级再决定要不要爬（只发少量探测请求，不下载任何图片）。"""
        if L.get("estimating"):
            return
        kws = [w for w in re.split(r"[\s,，]+", kw_var.get().strip()) if w]
        if not kws:
            messagebox.showinfo("提示", "请先输入关键词，再点「预估」")
            return
        if not picked_levels():
            messagebox.showinfo("提示", "「内容」里至少要勾一个分级")
            return
        L["estimating"] = True
        est_btn.configure(state="disabled")
        status_var.set("正在预估…（会发少量探测请求，不下载图片）")
        logln("")
        logln("=" * 62)
        logln(f"[预估] 关键词：{'、'.join(kws)}")
        sub_cfg = gui_cfg_snapshot()
        order = str(order_var.get())
        levels = sub_cfg.get("r18_levels") or ()
        ai_mode = str(sub_cfg.get("ai_mode") or "all")
        rng = (sub_cfg.get("date_from") or "", sub_cfg.get("date_to") or "")
        # 逐作品详情只在有人气门槛时才需要（点赞/收藏数不在搜索列表里）
        need_detail = bool(int(min_likes_var.get() or 0) or int(min_bm_var.get() or 0))

        def worker() -> None:
            try:
                wb = measure_local_work_bytes(L["lib"])
                if wb:
                    msg_q.put(f"[预估] 按本机图库实测：每作品平均 {wb / 1048576:.2f} MB")
                sub = dict(sub_cfg)
                # 预估只在第一个关键词上做（多个关键词各自量级相近，逐个算太慢）
                est = estimate_crawl(sub, keyword=kws[0], start=parse_seg_date(rng[0]),
                                     end=parse_seg_date(rng[1]), order=order,
                                     mode=str(sub_cfg.get("mode") or "all"),
                                     s_mode=str(sub_cfg.get("s_mode") or "s_tag"),
                                     work_bytes=wb, need_detail=need_detail,
                                     log=lambda m: msg_q.put(f"[预估] {m}"))
                if ai_mode != "all" and est.get("works"):
                    # AI 占比未知，按常识给个范围提示（不假装精确）
                    msg_q.put("[预估] 注：AIGC 过滤后会明显少于上面的数字"
                              "（AI 作品在多数标签下只占少数），实际以爬取结果为准。")
                try:
                    free = shutil.disk_usage(native_path(L["lib"].root)).free
                except OSError:
                    free = 0
                for line in estimate_summary(est, disk_free=free):
                    msg_q.put("[预估] " + line)
                if len(kws) > 1:
                    msg_q.put(f"[预估] 注：以上只估了「{kws[0]}」一个关键词；"
                              f"另有 {len(kws) - 1} 个关键词，总量大致按倍数增加")
                msg_q.put(("__EST_DONE__", est))
            except Exception as exc:  # noqa: BLE001
                msg_q.put(f"[预估] 失败：{type(exc).__name__}: {exc}")
                msg_q.put(("__EST_DONE__", {}))
        threading.Thread(target=worker, daemon=True).start()

    def pump() -> None:
        """唯一的日志泵：把后台线程的消息渲染到界面（主线程）。"""
        try:
            while True:
                msg = msg_q.get_nowait()
                if msg == DONE:
                    L["busy"] = False
                    status_var.set("就绪")
                    crawl_prog.configure(value=0)
                    crawl_prog_var.set("就绪")
                    L.pop("stop_evt", None)
                    # 记录本次爬取的新作品，供「标记这批图」按钮用（读爬取历史最后一条）
                    try:
                        hist = load_crawl_history(L["lib"].index_dir)
                        last = hist[-1] if hist else {}
                        L["last_crawl_works"] = list(last.get("new_works") or [])
                    except Exception:  # noqa: BLE001
                        L["last_crawl_works"] = []
                    batch_dtags_btn.configure(state="normal")
                    refresh_list()
                    load_tags()
                    load_dtag_list()
                    load_aid_list()
                    run_search()
                    refresh_history_tab()
                    continue
                if isinstance(msg, tuple):
                    # 追更页消息：(tag='[follow]', text) / ('__FOLLOW_DONE__', None)
                    if msg[0] == "[follow]":
                        flogln(msg[1])
                        continue
                    if msg[0] == "__PROG__":
                        _render_crawl_progress(msg[1] if len(msg) > 1 else {})
                        continue
                    if msg[0] == "__FPROG__":
                        _render_follow_progress(msg[1] if len(msg) > 1 else {})
                        continue
                    if msg[0] == "__FOLLOW_DONE__":
                        L["busy"] = False
                        follow_status_var.set("同步完成")
                        f_prog.configure(value=0)
                        f_prog_var.set("就绪")
                        L.pop("stop_evt", None)
                        refresh_follows()
                        refresh_list()
                        load_tags()
                        load_dtag_list()
                        load_aid_list()
                        run_search()
                        refresh_history_tab()
                        # 同步完成后跳转到「图库检索」页，自动按这批画师筛选，
                        # 让用户立刻看到刚同步下来的作品（干跑时不跳）。
                        # 关键：先清空上次的检索词/标签/时间/人气/分级，否则旧筛选
                        # 会叠加在画师筛选上，可能显示 0 条，让用户误以为同步失败。
                        if L.get("goto_search_after_follow") and L.get("last_sync_artists"):
                            reset_search()
                            q_aid_var.set(", ".join(L["last_sync_artists"]))
                            nb.select(nb.tabs()[2])
                            run_search()
                        L.pop("goto_search_after_follow", None)
                        L.pop("last_sync_artists", None)
                        continue
                    kind, payload = msg
                    if kind == "__PROBE_TICK__":
                        cap_banner.set(f"正在检测… {payload}")
                        continue
                    if kind == "__PROBE_DONE__":
                        L["probing"] = False
                        cap_probe_btn.configure(state="normal")
                        render_capability(payload or {})
                        continue
                    if kind == "__EST_DONE__":
                        L["estimating"] = False
                        est_btn.configure(state="normal")
                        status_var.set("预估完成")
                        est = payload or {}
                        if est.get("works"):
                            messagebox.showinfo(
                                "预估结果（数量级）",
                                "\n".join(estimate_summary(
                                    est, disk_free=est.get("disk_free", 0)))
                                + "\n\n详细数据见下方日志。")
                        continue
                    if kind == "__COOKIE_OK__":
                        cap_status_var.set(
                            f"已从浏览器读取并保存（{payload} 字符）→ 保存位置 {CRED_FILE}，正在重新检测…")
                        start_probe()
                        continue
                    if kind == "__COOKIE_FAIL__":
                        cap_status_var.set(
                            "没能自动读取浏览器 cookie。常见原因：\n"
                            "  · Edge/Chrome 127+ 启用了「应用绑定加密」，非管理员权限读不到密钥\n"
                            "  · 浏览器正在运行且 cookie 尚未落盘\n"
                            "  · 用的浏览器不在支持列表内\n"
                            "→ 请改用下面的「手动填 PHPSESSID…」，或「用 pixiv 官网登录（OAuth）」。")
                        continue
                    if kind == "__OAUTH_OK__":
                        cap_status_var.set(f"OAuth 登录成功：{payload}。refresh_token 已保存到 "
                                           f"{CRED_FILE}，正在重新检测…")
                        start_probe()
                        continue
                    if kind == "__OAUTH_FAIL__":
                        cap_status_var.set(f"OAuth 登录失败：{payload}\n"
                                           "常见原因：授权码已过期（页面停留过久）、未完整复制回调地址、"
                                           "或网络需要代理。点「取消」后重新打开授权页再试。")
                        continue
                logln(msg)
        except queue.Empty:
            pass
        root.after(120, pump)

    crawl_btns = ttk.Frame(tab_crawl, padding=(0, 4, 0, 0))
    # 固定到底部：如果按默认"顶部"顺序且不加 side，日志区(fill=both, expand=True)
    # 会把剩余空间全吞掉，按钮行被挤成 1px、看不见（实测踩到）。
    crawl_btns.pack(fill="x", side="bottom")

    # 进度行：进度条 + 当前任务指示（爬取页进度）
    crawl_prog_row = ttk.Frame(tab_crawl, padding=(0, 0, 0, 4))
    crawl_prog_row.pack(fill="x", side="bottom")
    crawl_prog = ttk.Progressbar(crawl_prog_row, maximum=100, value=0, length=320)
    crawl_prog.pack(side="left", fill="x", expand=True)
    crawl_prog_var = tk.StringVar(value="就绪")
    ttk.Label(crawl_prog_row, textvariable=crawl_prog_var, foreground="#666",
              width=46, anchor="w").pack(side="left", padx=(8, 0))

    ttk.Button(crawl_btns, text="开始爬取", command=start_crawl).pack(side="left")
    # 预估排在"开始爬取"旁边 —— 建议先点它看看量级（几 MB 还是几 TB）
    est_btn = ttk.Button(crawl_btns, text="预估（建议先点）", command=start_estimate)
    est_btn.pack(side="left", padx=(6, 0))
    ttk.Button(crawl_btns, text="停止", command=lambda: stop_crawl()).pack(side="left", padx=(10, 0))
    # 标记这批图：爬取完成后可点（给本次新下载的作品批量加衍生标签）
    batch_dtags_btn = ttk.Button(crawl_btns, text="标记这批图…", state="disabled",
                                 command=lambda: mark_batch_dtags())
    batch_dtags_btn.pack(side="left", padx=(6, 0))
    ttk.Label(crawl_btns, textvariable=status_var).pack(side="left", padx=12)
    ttk.Button(crawl_btns, text="打开图库目录",
               command=lambda: open_path(L["lib"].root)).pack(side="right", padx=2)
    ttk.Button(crawl_btns, text="清空日志",
               command=lambda: log.delete("1.0", "end")).pack(side="right", padx=2)
    kw_entry.bind("<Return>", lambda _e: start_crawl())

    refresh_lib_info()
    root.after(120, pump)
    root.mainloop()
    return 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser(program_dir: Path) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pixiv_crawler.py",
        description="pixiv 关键词爬虫：搜索 -> 下载原图 -> 按标签/画师分类 -> 本地可检索",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python pixiv_crawler.py crawl 初音ミク --pages 2\n"
            "  python pixiv_crawler.py crawl 風景 --order popular --limit 100\n"
            "  python pixiv_crawler.py                 # 交互式输入关键词\n"
            "  python pixiv_crawler.py search 初音 --tag VOCALOID\n"
            "  python pixiv_crawler.py search --list-tags 30\n"
            "  python pixiv_crawler.py stats\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"pixiv_crawler {__version__}")

    def add_common(parser: argparse.ArgumentParser) -> None:
        """通用参数：子命令前后都允许写（parents 会让两处生效）。"""
        parser.add_argument("--config", help="指定 config.json 路径")
        parser.add_argument("--out", help="图库输出目录（默认 D:\\PixivCrawler\\library）")
        parser.add_argument("--cookie", help="PHPSESSID（覆盖配置）")
        parser.add_argument("--refresh-token", dest="refresh_token", help="pixiv refresh_token（覆盖配置）")
        parser.add_argument("--proxy", help="HTTP 代理，如 http://127.0.0.1:7890")
        parser.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（代理自签证书时用）")
        parser.add_argument("--concurrency", type=int, help="并发下载数（默认 4）")
        parser.add_argument("--rps", type=float, help="每秒请求数上限（默认 1.2）")
        parser.add_argument("--timeout", type=float, help="单请求超时秒数")
        parser.add_argument("--keep-r18", action="store_true",
                            help="收 R-18 作品（R-18G 猎奇内容仍然跳过）")
        parser.add_argument("--keep-r18g", action="store_true",
                            help="连 R-18G（猎奇/グロ，xRestrict=2）也收；不指定时 R-18G 始终跳过")
        parser.add_argument("--date-from", dest="date_from", metavar="时间",
                            help="只收此时间之后发布的作品：2024（整年）/ 2024-07（整月）/ "
                                 "2024-07-15（当天）/ \"2024-07-15 18:30\"（精确到分钟）")
        parser.add_argument("--date-to", dest="date_to", metavar="时间",
                            help="只收此时间之前发布的作品（含当天）；写 2024 表示整个 2024 年")
        parser.add_argument("--min-likes", dest="min_likes", type=int, metavar="N",
                            help="只收点赞数（いいね）≥ N 的作品（0=不限制）")
        parser.add_argument("--min-bookmarks", dest="min_bookmarks", type=int, metavar="N",
                            help="只收收藏数（ブックマーク）≥ N 的作品（0=不限制）")
        parser.add_argument("--keep-ugoira", action="store_true", help="下载动图 ugoira（默认跳过）")
        parser.add_argument("--ugoira-format", dest="ugoira_format",
                            choices=["none", "webp", "gif", "mp4", "webm"],
                            help="动图转成什么格式（需要 ffmpeg；none=只保留原始 zip）")
        parser.add_argument("--drop-ugoira-zip", dest="drop_ugoira_zip", action="store_true",
                            help="动图转码成功后删除原始 zip（省空间；默认保留）")
        parser.add_argument("--max-pages-per-work", type=int, dest="max_pages_per_work",
                            help="每个作品最多下载几页（0=全部）")
        parser.add_argument("-v", "--verbose", action="store_true", help="显示详细配置信息")

    add_common(p)
    common = argparse.ArgumentParser(add_help=False)
    add_common(common)

    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("crawl", help="按关键词爬取并下载原图", parents=[common])
    c.add_argument("keywords", nargs="*", help="一个或多个关键词")
    c.add_argument("--pages", type=int, help="翻页数（每页约 59 个作品）")
    c.add_argument("--order", choices=["date", "popular", "old", "popular_male", "popular_female"],
                   help="排序：date=最新, popular=热门, old=最早")
    c.add_argument("--mode", choices=["all", "safe", "r18"], help="内容筛选")
    c.add_argument("--s-mode", dest="s_mode", choices=["tag", "tag_full", "text"],
                   help="匹配方式：tag=标签部分一致, tag_full=标签完全一致, text=标题/说明")
    c.add_argument("--limit", type=int, help="本次最多处理多少个作品")
    c.add_argument("--deep", action="store_true",
                   help="深度模式：自动组合「最新+最早」等多种排序，突破单一排序约 600 个作品的接口上限")
    c.add_argument("--deep-all", dest="deep_all", action="store_true",
                   help="深度模式再加热门/男性向/女性向等组合（实测多为重复，一般不必用）")
    c.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="干跑侦察：只统计各组合能取到多少作品，不下载任何文件")
    c.add_argument("--force", action="store_true", help="已下载过的作品也重新下载")
    c.add_argument("--restart", action="store_true",
                   help="清空爬取进度记忆，所有搜索组合从头重翻（默认会跳过上次已爬完的组合）")
    c.add_argument("--r18", dest="r18_levels", metavar="LEVELS",
                   help="内容分级，自由组合：全年龄用 all-ages/0，R-18 用 r18/1，"
                        "R-18G 用 r18g/2；用逗号分隔。例：--r18 0,1 表示全年龄+R-18；"
                        "--r18 2 表示只要 R-18G；--r18 none 表示都不要")
    c.add_argument("--ai-mode", dest="ai_mode", choices=["all", "exclude", "only"],
                   help="AIGC：all=不限（默认），exclude=排除 AI 生成，only=只要 AI 生成")
    c.add_argument("--artist", dest="artist_ids", action="append", metavar="ID或链接",
                   help="只收这些画师的作品，可重复；支持纯数字 ID 或主页链接")
    c.add_argument("--full", action="store_true",
                   help="全量模式：按时间段递归切分，突破单一查询约 6180 条的翻页上限，"
                        "把该关键词的结果尽量全取下来（很慢、占空间大，建议先 --estimate）")
    c.add_argument("--full-from", dest="full_from",
                   help="全量模式的起始日期（YYYY-MM-DD，默认从 pixiv 最早的 2007-09-01 起）")
    c.add_argument("--full-to", dest="full_to",
                   help="全量模式的结束日期（YYYY-MM-DD，默认到今天）")
    c.add_argument("--segment-target", dest="segment_target", type=int,
                   help=f"全量模式每段的目标条数（默认 {SEG_TARGET}，越小分得越细）")
    c.add_argument("-i", "--interactive", action="store_true", help="交互式输入关键词")
    c.add_argument("--from-bookmarks", dest="from_bookmarks", action="append", metavar="用户ID",
                   help="收藏夹模式：爬取该用户公开收藏夹里的作品（网页接口，任何登录态可用）")
    c.add_argument("--bookmark-rest", dest="bookmark_rest", choices=["show", "hide"],
                   help="收藏夹可见性：show=公开（默认），hide=私密（仅本人，需要 refresh_token）")
    c.add_argument("--bookmark-tag", dest="bookmark_tag",
                   help="只爬收藏夹里带这个收藏标签的作品（默认全部）")

    e = sub.add_parser("estimate", help="预估：算出一共能爬多少、要多大空间、要多久",
                       parents=[common])
    e.add_argument("keyword", nargs="?", help="关键词（不给则交互输入）")
    e.add_argument("--order", choices=["date", "popular", "old"], default="date", help="排序")
    e.add_argument("--mode", choices=["all", "safe", "r18"], help="内容筛选")
    e.add_argument("--s-mode", dest="s_mode", choices=["tag", "tag_full", "text"])
    e.add_argument("--r18", dest="r18_levels", metavar="LEVELS",
                   help="内容分级，自由组合（同 crawl 的 --r18）：0=全年龄, 1=R-18, 2=R-18G")
    e.add_argument("--probe-requests", dest="probe_requests", type=int, default=0,
                   help="最多发多少次探测请求（默认 40，越大越准也越慢）")
    e.add_argument("--work-mb", dest="work_mb", type=float,
                   help="每个作品平均体积 MB（默认按本机图库实测，无样本时用 5.5）")
    e.add_argument("--throughput", type=float,
                   help=f"单流下载速度 MB/s（默认 {DEFAULT_THROUGHPUT}，保守估计）")
    e.add_argument("-i", "--interactive", action="store_true", help="交互式输入关键词")

    s = sub.add_parser("search", help="在本地图库中检索", parents=[common])
    s.add_argument("words", nargs="*", help="检索词（标题/画师/标签/关键词/ID 模糊匹配）")
    s.add_argument("--tag", action="append", help="按标签筛选（可重复）")
    s.add_argument("--author", help="按画师筛选（模糊）")
    s.add_argument("--artist-id", dest="artist_ids", action="append", metavar="ID或链接",
                   help="按画师 ID 精确筛选，可重复（支持纯数字 ID 或主页链接）")
    s.add_argument("--query", help="按当初爬取用的关键词筛选")
    s.add_argument("--id", action="append", help="按作品 ID 精确筛选（可重复）")
    s.add_argument("--limit", type=int, default=50, help="显示条数上限，0=全部")
    s.add_argument("--list-tags", type=int, dest="list_tags", help="列出图库中的高频标签")
    s.add_argument("--export", help="把结果导出为 CSV")
    s.add_argument("--open-first", action="store_true", help="用系统看图程序打开第一条结果")
    s.add_argument("--full-path", action="store_true", help="显示文件的绝对路径")
    s.add_argument("-i", "--interactive", action="store_true", help="交互式输入检索词")

    sub.add_parser("stats", help="图库统计", parents=[common])
    fo = sub.add_parser("follow", help="画师追更：订阅清单 + 增量下载新作品", parents=[common])
    fo.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "remove", "sync"], help="默认 list")
    fo.add_argument("targets", nargs="*",
                    help="add/remove：画师 ID 或主页链接；sync：只追更指定画师（可省略=全部）")
    fo.add_argument("--name", help="add 时顺便记录画师名（方便日后辨认）")
    fo.add_argument("--note", help="add 时写备注")
    fo.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="sync 时只列出会下载哪些新作品，不实际下载")
    fo.add_argument("--limit", type=int, default=0, help="每个画师本次最多处理多少个新作品")
    fo.add_argument("--order", choices=["date", "old"], default="date",
                    help="新作品的处理顺序：date=从新到旧（默认），old=从旧到新")
    fo.add_argument("--pages", type=int, help="保留参数（追更按画师作品列表全量比对，不受翻页限制）")
    au = sub.add_parser("auth", help="登录 pixiv / 查看状态 / 体检读取能力 / 退出登录", parents=[common])
    au.add_argument("action", nargs="?", default="status",
                    choices=["login", "status", "doctor", "logout"], help="默认 status")
    au.add_argument("--method", choices=["auto", "token", "cookie", "auto-cookie"],
                    help="登录方式：token=OAuth 拿 refresh_token（推荐）；"
                         "auto-cookie=自动读本机浏览器；cookie=手动粘贴 PHPSESSID")
    au.add_argument("--code", help="直接提供授权码或回调地址（跳过交互输入）")
    au.add_argument("--offline", action="store_true", help="status/doctor 时不联网")
    au.add_argument("--probe-pages", dest="probe_pages", type=int, default=0,
                    help="doctor 时探测翻多少页（默认 24，越大越准也越慢）")
    au.add_argument("--skip-anon", dest="skip_anon", action="store_true",
                    help="doctor 时跳过匿名对比（更快，但看不出提升倍数）")
    rp = sub.add_parser("repair", help="按索引补齐缺失的页/文件（不依赖搜索结果）", parents=[common])
    rp.add_argument("--dry-run", dest="dry_run", action="store_true", help="只列出缺什么，不下载")
    rp.add_argument("--limit", type=int, default=0, help="本次最多修复多少个作品（0=全部）")
    rp.add_argument("--clean-tags", action="store_true",
                    help="只清洗拼接标签（如 '初音ミク,' 拆成 '初音ミク'）后退出")
    st = sub.add_parser("selftest", help="自检：环境/配置/网络/凭据/索引（不下载图片）", parents=[common])
    st.add_argument("--offline", action="store_true", help="跳过网络测试，只做本地检查")
    r = sub.add_parser("reindex", help="重建 CSV/Markdown/SQLite 索引", parents=[common])
    r.add_argument("--relink", action="store_true", help="同时补齐缺失的分类硬链接")
    sub.add_parser("gui", help="启动图形界面（需要 tkinter）", parents=[common])
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    # 打包成 exe 时，__file__ 指向 PyInstaller 的 _internal 临时目录，
    # 但 config.json / artists.py / ugoira.py / browser_cookie.py 与 exe 放同一目录。
    # 因此：frozen 时一律把「exe 所在目录」当作程序目录。
    if getattr(sys, "frozen", False):
        program_dir = Path(sys.executable).resolve().parent
    else:
        program_dir = Path(__file__).resolve().parent
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser(program_dir)
    # 兼容 “python pixiv_crawler.py 初音ミク” 这种省略子命令的写法
    known = {"crawl", "search", "stats", "reindex", "gui", "selftest", "repair",
             "auth", "follow", "estimate"}
    if argv and not argv[0].startswith("-") and argv[0] not in known:
        argv = ["crawl"] + argv
    args = parser.parse_args(argv)

    if args.command == "auth":
        return cmd_auth(args, program_dir)
    if args.command == "follow":
        return cmd_follow(args, program_dir)
    if args.command == "search":
        return cmd_search(args, program_dir)
    if args.command == "stats":
        return cmd_stats(args, program_dir)
    if args.command == "repair":
        return cmd_repair(args, program_dir)
    if args.command == "selftest":
        return cmd_selftest(args, program_dir)
    if args.command == "reindex":
        return cmd_reindex(args, program_dir)
    if args.command == "gui":
        return cmd_gui(args, program_dir)
    if args.command == "crawl":
        return cmd_crawl(args, program_dir)
    if args.command == "estimate":
        return cmd_estimate(args, program_dir)
    return cmd_crawl(parser.parse_args(["crawl"]), program_dir)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        out("\n已中断。")
        sys.exit(130)
