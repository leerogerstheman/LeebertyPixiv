# -*- coding: utf-8 -*-
"""收藏夹爬取（crawl_bookmarks）核心逻辑回归测试（不联网，mock 收集器）。

覆盖：
  1. collect_bookmarks 返回空 -> 空结果不崩
  2. 列表阶段筛选：R18 / AI / 画师 / 已下载 都拦得住
  3. dry_run 只报数字不下载
  4. 全量处理循环可用（FakeClient 提供详情/下载）
"""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("pc", r"D:\PixivCrawler\pixiv_crawler.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

fails = []


def check(name, ok, extra=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
    if not ok:
        fails.append(name)


class FakeClient:
    """假的客户端：collect_bookmarks 返回构造的简表，详情/下载直接造一个记录。"""

    def __init__(self, briefs):
        self._briefs = briefs

    def collect_bookmarks(self, *a, **k):
        return list(self._briefs), len(self._briefs)

    def illust_detail(self, iid):
        return {"id": iid, "title": "t", "tags": [{"name": "x"}],
                "userName": "u", "userId": "1"}

    def resolve_original_urls(self, iid, detail=None):
        return [{"url": f"https://i.pximg.net/x/{iid}.png", "ext": ".png",
                 "width": 100, "height": 100}]


import tempfile, shutil  # noqa: E402

print("=== 1) 空收藏夹 ===")
cfg, _ = pc.load_config(Path(r"D:\PixivCrawler"), None)
tmp = Path(tempfile.mkdtemp(prefix="bm_test_"))
try:
    lib = pc.Library(tmp / "library")
    lib.ensure()
    s = pc.CrawlSession(dict(cfg), lib, verbose=False)
    s.client = FakeClient([])
    r = s.crawl_bookmarks("1", dry_run=False)
    check("空收藏夹返回 0 结果", r.get("new") == 0, r)

    print("\n=== 2) 列表阶段筛选 ===")
    s2 = pc.CrawlSession(dict(cfg), lib, verbose=False)
    s2.known = {"1000": 1}                    # 1000 已下载
    s2.r18_levels = (0,)                       # 只要全年龄
    s2.ai_exclude = True                       # 排除 AI
    s2.artist_ids = {"2"}                      # 只要画师 2
    s2.client = FakeClient([
        {"id": "1000", "xRestrict": 0, "aiType": 0, "userId": "2"},   # 已下载 → 跳过
        {"id": "1001", "xRestrict": 1, "aiType": 0, "userId": "2"},   # R18 → 跳过
        {"id": "1002", "xRestrict": 0, "aiType": 1, "userId": "2"},   # AI → 跳过
        {"id": "1003", "xRestrict": 0, "aiType": 0, "userId": "3"},   # 画师错 → 跳过
        {"id": "1004", "xRestrict": 0, "aiType": 0, "userId": "2"},   # 唯一该留下的
    ])
    r2 = s2.crawl_bookmarks("1", dry_run=True)
    check("筛选后只剩 1 个", r2.get("new") == 1, r2)

    print("\n=== 3) dry_run 不下载 ===")
    check("dry_run downloaded=0", r2.get("downloaded") == 0)

    print("\n=== 4) 全量处理循环（FakeClient 下载）===")
    s3 = pc.CrawlSession(dict(cfg), lib, verbose=False)
    s3.known = {}
    s3.client = FakeClient([
        {"id": "2001", "xRestrict": 0, "aiType": 0, "userId": "2",
         "pageCount": 1, "illustType": "illust", "title": "a"},
    ])
    s3.cfg["concurrency"] = 2
    from pathlib import Path as P
    orig_exists = P.is_file

    def fake_exists(self):
        return False
    P.is_file = fake_exists
    try:
        r3 = s3.crawl_bookmarks("1", dry_run=False)
        check("正常处理完成（new>=1）", r3.get("new") >= 1, r3)
    finally:
        P.is_file = orig_exists
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
sys.exit(0 if not fails else 1)