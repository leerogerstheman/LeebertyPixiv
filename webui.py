# -*- coding: utf-8 -*-
"""网页界面：用标准库 http.server 提供界面，浏览器打开即用。

与图形界面（tkinter）的关系
---------------------------
pixiv_crawler.py 里的 tkinter 界面原样保留，两者并存：

    gui.bat / python pixiv_crawler.py gui    桌面版
    web.bat / python webui.py                网页版（本文件）

两者共用同一套逻辑：检索、统计、重建索引、导出、爬取全部复用 pixiv_crawler.py，
一个字都没改。所以网页版看到的检索结果与统计数字与桌面版一致。

顺带一提：pixiv_crawler.py 里的 `import tkinter` 在函数内部，所以**导入模块本身
不需要 tkinter**（已实测：用一个没编 tkinter 的解释器 import 成功）。这让网页版在
没有 tkinter 的环境里也能跑。

为什么不用 pywebview
--------------------
本项目的依赖清单里只有标准库 + 可选的 ffmpeg（README：「无需 pip install 任何第三方
包」）。pywebview 会带进它自己的一串依赖。而界面需要的只是「把 HTML 给浏览器看」，
http.server 是标准库，一行依赖都不用加。

另外：PIL 在本项目里只用于 tkinter 的 ImageTk（预览图）。网页版不走那条路——图片由
浏览器自己渲染——所以网页版连 Pillow 都不需要。

爬取是长任务
------------
run_crawl(...) 接受 stop_event 与 progress_cb。网页版把 progress_cb 收到的东西存进
有界日志缓冲与计数快照，前端轮询 /api/crawl/status。停止走 stop_event.set()，是安全
停止——已下载的图和索引都会保存，下次自动续跑。

接口
----
    GET  /                    界面
    GET  /app.css /app.js     静态资源
    GET  /api/meta            配置摘要、图库统计、标签/画师、爬取历史、分级选项
    POST /api/search          图库检索（走 search_records_advanced）
    GET  /api/record          单条作品详情
    POST /api/crawl/start     开始爬取（后台线程）
    POST /api/crawl/stop      安全停止
    GET  /api/crawl/status    进度、计数、日志
    GET  /api/library/stats   图库统计
    POST /api/library/reindex 重建索引
    POST /api/export          导出（markdown / csv / sqlite）
    POST /api/config          保存设置

运行
----
    python webui.py                    默认端口 8762，自动打开浏览器
    python webui.py --port 9000 --no-browser
    python webui.py --out D:\\另一份图库
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pixiv_crawler as px

PROGRAM_DIR = Path(__file__).resolve().parent
WEBUI_DIR = PROGRAM_DIR / "webui"

LOG_LIMIT = 400
SEARCH_PAGE = 300


def log_line(text: str) -> None:
    try:
        p = PROGRAM_DIR / "logs" / "webui.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:      # noqa: BLE001
        pass


# ---------------------------------------------------------------- 状态 --

class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cfg: dict = {}
        self.lib = None
        self.stop_event: threading.Event | None = None
        self.thread: threading.Thread | None = None
        self.running = False
        self.counters = {"downloaded": 0, "skipped": 0, "failed": 0,
                         "skipped_r18": 0, "skipped_r18g": 0, "skipped_ugoira": 0}
        self.stage = ""
        self.done = 0
        self.total = 0
        self.detail = ""
        self.log: deque = deque(maxlen=LOG_LIMIT)
        self.result: dict | None = None
        self.error = ""
        self.started_at = 0.0
        self.last_search: list = []
        self._recs_cache: list | None = None

    def records(self, refresh: bool = False) -> list:
        with self.lock:
            if self._recs_cache is None or refresh:
                self._recs_cache = self.lib.load_records()
            return self._recs_cache

    def invalidate(self) -> None:
        with self.lock:
            self._recs_cache = None


STATE = State()


def _say(text: str) -> None:
    with STATE.lock:
        STATE.log.append(text)
    log_line(text)


def _bootstrap(out_dir: str = "") -> None:
    args = argparse.Namespace(out=out_dir or None)
    cfg, lib = px.prepare_cfg(args, PROGRAM_DIR)
    with STATE.lock:
        STATE.cfg = cfg
        STATE.lib = lib
    log_line("webui 启动 · 图库 %s" % cfg.get("output_dir"))


# ---------------------------------------------------------------- 元信息 --

def _safe_config() -> dict:
    """给界面看的配置摘要。cookie / token 只回是否已填，绝不回明文。"""
    cfg = STATE.cfg
    return {
        "outputDir": cfg.get("output_dir", ""),
        "requestsPerSecond": cfg.get("requests_per_second"),
        "concurrency": cfg.get("concurrency"),
        "timeout": cfg.get("timeout"),
        "maxRetries": cfg.get("max_retries"),
        "searchDelay": cfg.get("search_delay"),
        "proxy": cfg.get("proxy") or "",
        "pages": cfg.get("pages"),
        "order": cfg.get("order") or "date",
        "mode": cfg.get("mode") or "all",
        "sMode": cfg.get("s_mode") or "s_tag",
        "searchType": cfg.get("search_type") or "all",
        "maxWorksPerRun": cfg.get("max_works_per_run") or 0,
        "skipR18": bool(cfg.get("skip_r18")),
        "skipR18g": bool(cfg.get("skip_r18g")),
        "skipUgoira": bool(cfg.get("skip_ugoira")),
        "ugoiraFormat": cfg.get("ugoira_format") or "none",
        "ffmpegPath": cfg.get("ffmpeg_path") or "",
        "dateFrom": cfg.get("date_from") or "",
        "dateTo": cfg.get("date_to") or "",
        "minLikes": cfg.get("min_likes") or 0,
        "minBookmarks": cfg.get("min_bookmarks") or 0,
        "maxPagesPerWork": cfg.get("max_pages_per_work") or 0,
        "hasCookie": bool(str(cfg.get("cookie_phpsessid") or "").strip()),
        "hasToken": bool(str(cfg.get("refresh_token") or "").strip()),
    }


def _library_stats(refresh: bool = False) -> dict:
    """按 cmd_stats 的口径算统计，数据仍来自 Library.load_records()。"""
    recs = STATE.records(refresh)
    if not recs:
        return {"works": 0, "images": 0, "authors": 0, "tags": 0,
                "bytes": 0, "bytesText": px.format_size(0),
                "diskFree": 0, "diskFreeText": "", "topTags": [], "byKeyword": [],
                "library": str(STATE.lib.root)}

    works = {str(r.get("id")) for r in recs}
    authors = {str(r.get("author")) for r in recs}
    tags: dict = {}
    kws: dict = {}
    for r in recs:
        for t in r.get("tags") or []:
            tags[str(t)] = tags.get(str(t), 0) + 1
        for k in r.get("query_keys") or ([r.get("query")] if r.get("query") else []):
            kws[str(k)] = kws.get(str(k), 0) + 1
    total = sum(int(r.get("bytes") or 0) for r in recs)

    try:
        disk = shutil.disk_usage(px.native_path(STATE.lib.root))
        disk_free, disk_text = disk.free, px.format_size(disk.free)
    except Exception:      # noqa: BLE001
        disk_free, disk_text = 0, ""

    def top(d: dict, n: int) -> list:
        return [{"value": k, "count": c} for k, c in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]

    return {
        "works": len(works), "images": len(recs), "authors": len(authors), "tags": len(tags),
        "bytes": total, "bytesText": px.format_size(total),
        "diskFree": disk_free, "diskFreeText": disk_text,
        "topTags": top(tags, 40), "byKeyword": top(kws, 20),
        "library": str(STATE.lib.root),
    }


def meta_payload() -> dict:
    try:
        r18 = [{"value": k, "levels": list(v),
                "label": "%s（%s）" % (k, "+".join(str(x) for x in v) or "空")}
               for k, v in px.R18_MODE_LEVELS.items()]
    except Exception:      # noqa: BLE001
        r18 = []

    hist = []
    try:
        hist = px.load_crawl_history(STATE.lib.index_dir) or []
    except Exception:      # noqa: BLE001
        pass

    dtags = []
    try:
        dtags = sorted((px.load_dtags(STATE.lib.index_dir) or {}).keys())
    except Exception:      # noqa: BLE001
        pass

    stats = _library_stats()
    return {
        "title": "LeebertyPixiv",
        "config": _safe_config(),
        "stats": stats,
        "r18Modes": r18,
        "topTags": stats.get("topTags", []),
        "dtags": dtags[:80],
        "history": [
            {"time": str(h.get("time") or ""), "keyword": str(h.get("keyword") or h.get("query") or ""),
             "new": h.get("new"), "total": h.get("total")}
            for h in (hist[-20:] if isinstance(hist, list) else [])
        ],
        "ffmpegFound": bool(_safe_config()["ffmpegPath"] and os.path.exists(_safe_config()["ffmpegPath"])),
    }


# ---------------------------------------------------------------- 检索 --

def _lst(payload: dict, key: str) -> list:
    v = payload.get(key) or []
    if isinstance(v, str):
        v = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
    return [str(x) for x in v]


def do_search(payload: dict) -> dict:
    recs = STATE.records()
    hits = px.search_records_advanced(
        recs,
        text=str(payload.get("text") or ""),
        tags=_lst(payload, "tags"),
        tags_match_all=bool(payload.get("tagsMatchAll", True)),
        author=str(payload.get("author") or ""),
        author_ids=_lst(payload, "authorIds"),
        query=str(payload.get("query") or ""),
        date_from=str(payload.get("dateFrom") or ""),
        date_to=str(payload.get("dateTo") or ""),
        min_likes=int(payload.get("minLikes") or 0),
        min_bookmarks=int(payload.get("minBookmarks") or 0),
        r18_mode=str(payload.get("r18Mode") or "hide"),
        ai_mode=str(payload.get("aiMode") or "all"),
        d_tags=_lst(payload, "dtags"),
        d_tags_match_all=bool(payload.get("dtagsMatchAll", True)),
        limit=0,
        sort_by=str(payload.get("sortBy") or "default"),
    )

    with STATE.lock:
        STATE.last_search = hits

    rows = [_row(r) for r in hits[:SEARCH_PAGE]]
    return {"ok": True, "total": len(hits), "shown": len(rows), "rows": rows}


def _key(r: dict) -> str:
    return "%s|%s" % (r.get("id") or "", r.get("page") or 0)


def _row(r: dict) -> dict:
    """表格用的投影。

    字段名取自 Library.build_csv 的表头（那是权威清单）：
    id / page / title / author / author_id / create_date / bookmark_count /
    like_count / view_count / x_restrict。写成 bookmarks / likes / xr 会永远取到 0。
    """
    return {
        "key": _key(r),
        "id": str(r.get("id") or ""),
        "page": px.as_int(r.get("page"), 0),
        # clean_title 是核心自己的标题清理（去换行、截断），与命令行/桌面版一致
        "title": px.clean_title(r.get("title") or "", 120),
        "author": r.get("author") or "",
        "authorId": str(r.get("author_id") or ""),
        "date": str(r.get("create_date") or ""),
        "xr": px.as_int(r.get("x_restrict"), 0),
        "ai": px.as_int(r.get("ai"), 0) != 0,
        "likes": px.as_int(r.get("like_count"), 0),
        "bookmarks": px.as_int(r.get("bookmark_count"), 0),
        "views": px.as_int(r.get("view_count"), 0),
        "tags": [str(t) for t in (r.get("tags") or [])][:12],
        "dtags": [str(t) for t in (r.get("dtags") or [])][:8],
        "pages": px.as_int(r.get("page_count"), 0),
        "bytes": px.as_int(r.get("bytes"), 0),
    }


def get_record(key: str) -> dict:
    with STATE.lock:
        hits = list(STATE.last_search)
    for r in hits:
        if _key(r) == key:
            return {"ok": True, "record": r}
    raise ValueError("找不到这条记录（可能已重新检索）")


# ---------------------------------------------------------------- 爬取 --

def _crawl_cfg(payload: dict) -> dict:
    """把网页表单收成一个爬取配置，键名与 gui_cfg_snapshot() 一致。"""
    sub = dict(STATE.cfg)
    sub["mode"] = str(payload.get("mode") or sub.get("mode") or "all")
    lvls = payload.get("r18Levels")
    if isinstance(lvls, list) and lvls:
        try:
            sub["r18_levels"] = tuple(int(x) for x in lvls)
        except Exception:      # noqa: BLE001
            pass
    sub["ai_mode"] = str(payload.get("aiMode") or "all")
    sub["artist_ids"] = _lst(payload, "artistIds")
    sub["date_from"] = str(payload.get("dateFrom") or "")
    sub["date_to"] = str(payload.get("dateTo") or "")
    sub["min_likes"] = int(payload.get("minLikes") or 0)
    sub["min_bookmarks"] = int(payload.get("minBookmarks") or 0)
    sub["max_pages_per_work"] = int(payload.get("maxPagesPerWork") or 0)
    sub["ugoira_format"] = str(payload.get("ugoiraFormat") or "none")
    sub["skip_ugoira"] = sub["ugoira_format"] == "none"
    return sub


def _crawl_worker(sub_cfg: dict, keywords: list, payload: dict) -> None:
    def on_progress(kw: dict) -> None:
        with STATE.lock:
            stage = kw.get("stage")
            if stage:
                STATE.stage = str(stage)
            for k in ("done", "total"):
                if k in kw:
                    setattr(STATE, k, int(kw.get(k) or 0))
            d = kw.get("detail")
            if d:
                STATE.detail = str(d)

    def on_log(msg) -> None:
        _say(str(msg))

    try:
        _say("开始爬取 · 关键词 %s" % " / ".join(keywords))
        res = px.run_crawl(
            sub_cfg, STATE.lib, keywords,
            pages=int(payload.get("pages") or 1),
            order=str(payload.get("order") or "date"),
            mode=str(sub_cfg.get("mode") or "all"),
            s_mode=str(sub_cfg.get("s_mode") or "s_tag"),
            max_works=int(payload.get("maxWorks") or 0),
            force=bool(payload.get("force")),
            deep=bool(payload.get("deep")),
            dry_run=bool(payload.get("dryRun")),
            r18_levels=sub_cfg.get("r18_levels"),
            ai_mode=str(sub_cfg.get("ai_mode") or "all"),
            artist_ids=sub_cfg.get("artist_ids") or None,
            scope=str(payload.get("scope") or "auto"),
            stop_event=STATE.stop_event,
            progress_cb=on_progress,
            log=on_log,
        )
        with STATE.lock:
            STATE.result = res if isinstance(res, dict) else {"result": str(res)}
        STATE.invalidate()
        _say("✓ 爬取结束")
    except Exception as exc:      # noqa: BLE001
        with STATE.lock:
            STATE.error = "%s: %s" % (type(exc).__name__, exc)
        _say("✗ 出错：%s: %s" % (type(exc).__name__, exc))
        log_line(traceback.format_exc())
    finally:
        with STATE.lock:
            STATE.running = False


def start_crawl(payload: dict) -> dict:
    keywords = _lst(payload, "keywords")
    if not keywords:
        raise ValueError("请先填关键词")
    with STATE.lock:
        if STATE.running:
            raise ValueError("已有爬取在进行中")
        sub_cfg = _crawl_cfg(payload)
        STATE.running = True
        STATE.error = ""
        STATE.result = None
        STATE.stage = "准备中"
        STATE.done = STATE.total = 0
        STATE.detail = ""
        STATE.counters = {k: 0 for k in STATE.counters}
        STATE.started_at = time.time()
        STATE.log.clear()
        STATE.stop_event = threading.Event()

    STATE.thread = threading.Thread(target=_crawl_worker,
                                    args=(sub_cfg, keywords, payload), daemon=True)
    STATE.thread.start()
    return {"ok": True, "keywords": keywords}


def crawl_status() -> dict:
    with STATE.lock:
        return {
            "running": STATE.running,
            "counters": dict(STATE.counters),
            "stage": STATE.stage,
            "done": STATE.done,
            "total": STATE.total,
            "detail": STATE.detail,
            "elapsed": round(time.time() - STATE.started_at, 1) if STATE.started_at else 0,
            "log": list(STATE.log),
            "result": STATE.result,
            "error": STATE.error,
        }


def stop_crawl() -> dict:
    with STATE.lock:
        ev = STATE.stop_event
    if ev is None or not STATE.running:
        return {"ok": True, "stopped": False, "message": "当前没有运行中的爬取"}
    ev.set()
    _say("已请求停止，正在保存…（已下载内容不会丢）")
    return {"ok": True, "stopped": True}


# ---------------------------------------------------------------- 图库 --

def do_reindex() -> dict:
    info = STATE.lib.rebuild_all()
    STATE.invalidate()
    return {"ok": True, "result": {k: (str(v) if isinstance(v, Path) else v) for k, v in info.items()}}


def do_export(payload: dict) -> dict:
    fmt = str(payload.get("format") or "markdown").lower()
    if fmt == "markdown":
        path = STATE.lib.export_markdown()
    elif fmt in ("csv", "sqlite"):
        info = STATE.lib.rebuild_all()
        key = fmt
        if key not in info:
            raise ValueError("这次重建没有产出 %s" % fmt)
        path = Path(str(info[key]))
    else:
        raise ValueError("不支持的导出格式：%s" % fmt)
    p = Path(str(path))
    return {"ok": True, "format": fmt, "path": str(p), "name": p.name,
            "size": p.stat().st_size if p.exists() else 0}


CONFIG_KEYS = {
    "requestsPerSecond": ("requests_per_second", float),
    "concurrency": ("concurrency", int),
    "timeout": ("timeout", int),
    "maxRetries": ("max_retries", int),
    "searchDelay": ("search_delay", float),
    "proxy": ("proxy", str),
    "pages": ("pages", int),
    "order": ("order", str),
    "mode": ("mode", str),
    "sMode": ("s_mode", str),
    "maxWorksPerRun": ("max_works_per_run", int),
    "ugoiraFormat": ("ugoira_format", str),
    "ffmpegPath": ("ffmpeg_path", str),
    "dateFrom": ("date_from", str),
    "dateTo": ("date_to", str),
    "minLikes": ("min_likes", int),
    "minBookmarks": ("min_bookmarks", int),
    "maxPagesPerWork": ("max_pages_per_work", int),
}


def do_save_config(payload: dict) -> dict:
    changed = {}
    with STATE.lock:
        cfg = STATE.cfg
        for web_key, (cfg_key, cast) in CONFIG_KEYS.items():
            if web_key not in payload:
                continue
            raw = payload.get(web_key)
            try:
                cfg[cfg_key] = cast(raw) if raw not in (None, "") else ("" if cast is str else 0)
            except Exception:      # noqa: BLE001
                raise ValueError("配置项 %s 的值不合法：%r" % (web_key, raw))
            changed[cfg_key] = cfg[cfg_key]
        if "skipUgoira" in payload:
            cfg["skip_ugoira"] = bool(payload.get("skipUgoira"))
            changed["skip_ugoira"] = cfg["skip_ugoira"]

    if not changed:
        return {"ok": True, "changed": {}}

    path = PROGRAM_DIR / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except Exception:      # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.update(changed)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _say("配置已保存：%s" % ", ".join(changed.keys()))
    return {"ok": True, "changed": changed}


# ---------------------------------------------------------------- HTTP --

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "PixivCrawlerWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):      # noqa: A003
        if os.environ.get("PX_WEB_VERBOSE"):
            sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _body(self, limit: int = 8 * 1024 * 1024) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:      # noqa: BLE001
            n = 0
        if n <= 0:
            return {}
        if n > limit:
            raise ValueError("请求体过大")
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:      # noqa: BLE001
            raise ValueError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是对象")
        return data

    def _static(self, rel: str) -> None:
        target = (WEBUI_DIR / rel).resolve()
        try:
            target.relative_to(WEBUI_DIR.resolve())
        except ValueError:
            self._error("禁止访问", 403)
            return
        if not target.is_file():
            self._error("找不到 %s" % rel, 404)
            return
        with open(target, "rb") as fh:
            self._send(200, fh.read(), CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))

    def do_GET(self):       # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path in ("/app.css", "/app.js"):
                self._static(path.lstrip("/"))
            elif path == "/api/meta":
                self._json(meta_payload())
            elif path == "/api/crawl/status":
                self._json(crawl_status())
            elif path == "/api/library/stats":
                self._json({"ok": True, "stats": _library_stats(refresh=True)})
            elif path == "/api/record":
                self._json(get_record((query.get("key") or [""])[0]))
            else:
                self._error("未知路径 %s" % path, 404)
        except Exception as exc:      # noqa: BLE001
            log_line(traceback.format_exc())
            self._error(str(exc), 500)

    def do_POST(self):      # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/search":
                self._json(do_search(self._body()))
            elif path == "/api/crawl/start":
                self._json(start_crawl(self._body()))
            elif path == "/api/crawl/stop":
                self._json(stop_crawl())
            elif path == "/api/library/reindex":
                self._json(do_reindex())
            elif path == "/api/export":
                self._json(do_export(self._body()))
            elif path == "/api/config":
                self._json(do_save_config(self._body()))
            else:
                self._error("未知路径 %s" % path, 404)
        except Exception as exc:      # noqa: BLE001
            log_line(traceback.format_exc())
            self._error(str(exc), 500)


# ---------------------------------------------------------------- 启动 --

def _free_port(preferred: int) -> int:
    for candidate in (preferred, 0):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", candidate))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError("找不到可用端口")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port, open_browser, out_dir = 8762, True, ""
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a == "--out" and i + 1 < len(argv):
            out_dir = argv[i + 1]
        elif a == "--no-browser":
            open_browser = False
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0

    if not WEBUI_DIR.is_dir():
        print("找不到界面目录：%s" % WEBUI_DIR)
        return 1

    _bootstrap(out_dir)
    port = _free_port(port)
    url = "http://127.0.0.1:%d/" % port

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True

    print()
    print("  " + "=" * 58)
    print("   %s · 网页版" % "LeebertyPixiv")
    print("  " + "=" * 58)
    print()
    print("   地址 / URL    %s" % url)
    print("   图库目录      %s" % STATE.cfg.get("output_dir"))
    print("   登录凭据      %s" % ("已配置" if _safe_config()["hasCookie"] or _safe_config()["hasToken"]
                                   else "未配置（只能看图库，不能爬取）"))
    print()
    print("   只监听本机。按 Ctrl+C 停止。")
    print()

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
