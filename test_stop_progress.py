# -*- coding: utf-8 -*-
"""停止/进度机制回归测试（核心层，纯本地验证语义）。

覆盖：
  1. should_stop / report 回调正常
  2. 手动置位 stop_event 后，爬取循环在安全点退出（用假进度回调验证）
  3. run_follow_sync 能接受 stop_event / progress_cb（GUI 共用入口）
"""
import importlib.util
import sys
import threading
from pathlib import Path

spec = importlib.util.spec_from_file_location("pc", r"D:\PixivCrawler\pixiv_crawler.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

fails = []


def check(name, ok, extra=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
    if not ok:
        fails.append(name)


print("=== 1) 停止/进度机制存在于 CrawlSession ===")
cfg, _ = pc.load_config(Path(r"D:\PixivCrawler"), None)
lib = pc.Library(Path(cfg["output_dir"]))
s = pc.CrawlSession(cfg, lib, verbose=False)
check("有 stop_event", isinstance(s.stop_event, threading.Event))
check("初始未停止", not s.should_stop())
seen = []
s.progress_cb = lambda kw: seen.append(kw)
s.report(stage="test", done=1, total=3, detail="x")
check("report 回调触发", len(seen) == 1 and seen[0]["done"] == 1, seen)
s.stop_event.set()
check("置位后 should_stop 为真", s.should_stop())


print("\n=== 2) 安全点退出：置位后 crawl_keyword 的组合循环立即停 ===")
# 用假搜索替换，验证循环会在第一个 stop 检查处退出
class FakeClient:
    artist_work_ids = lambda self, *a, **k: ([], {})


s2 = pc.CrawlSession(cfg, lib, verbose=False)
s2.known = {}
s2.stop_event.set()          # 一开始就停止
s2.client = FakeClient()

# 直接调 crawl_keyword 的入口（组合循环第一件事就是 should_stop）
stop_logs = []


def fake_out(m):
    stop_logs.append(str(m))


orig_out = pc.out
pc.out = fake_out
try:
    r = s2.crawl_keyword("测试", pages=1, order="date", mode="all", s_mode="s_tag",
                         max_works=0, dry_run=True)
finally:
    pc.out = orig_out
check("置位后立即退出（无收藏品被扫描）", r.get("works") == 0)
check("日志中有停止说明", any("停止" in m for m in stop_logs), stop_logs[:1])


print("\n=== 3) run_follow_sync 接受 stop_event/progress_cb ===")
store = pc.artists_mod.ArtistStore(lib.root / pc.artists_mod.ARTISTS_FILENAME)
evt = threading.Event()
got = []


def cb(kw):
    got.append(kw)


# 空清单 + 预置停止 -> 应快速返回，不报错
import json
orig_artists = store.artists
store.artists = []
try:
    r = pc.run_follow_sync(dict(cfg), lib, store, [], dry_run=True,
                           stop_event=evt, progress_cb=cb, log=lambda m: None)
    check("空清单快速返回", isinstance(r, dict))
finally:
    store.artists = orig_artists

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
sys.exit(0 if not fails else 1)