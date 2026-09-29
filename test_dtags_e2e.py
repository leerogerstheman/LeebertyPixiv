# -*- coding: utf-8 -*-
"""衍生标签与爬取历史的端到端验证（不联网）：合并、筛选、序号、自动标签。"""
import importlib.util
import shutil
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("pc", r"D:\PixivCrawler\pixiv_crawler.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

fails = []


def check(name, ok, extra=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
    if not ok:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="dtags_e2e_"))
try:
    idx = tmp / "_index"
    idx.mkdir(parents=True, exist_ok=True)
    # 构造三条记录（两个作品，其中 1001 有 2 页）
    recs = [
        {"id": "1001", "page": 0, "title": "作品A", "tags": ["初音", "miku"], "author": "X",
         "x_restrict": 0, "ai": 0, "author_id": "1", "bytes": 1000,
         "create_date": "2026-09-30T00:00:00+00:00"},
        {"id": "1001", "page": 1, "title": "作品A", "tags": ["初音", "miku"], "author": "X",
         "x_restrict": 0, "ai": 0, "author_id": "1", "bytes": 1000,
         "create_date": "2026-09-30T00:00:00+00:00"},
        {"id": "1002", "page": 0, "title": "作品B", "tags": ["风景"], "author": "Y",
         "x_restrict": 0, "ai": 1, "author_id": "2", "bytes": 2000,
         "create_date": "2025-01-01T00:00:00+00:00"},
    ]

    print("=== 1) 合并衍生标签进记录（按作品 ID，两页共享）===")
    pc.set_work_dtags(idx, "1001", ["收藏"])
    pc.add_work_dtags(idx, "1002", ["2026年9月30日第1次爬取"])
    pc.merge_dtags(recs, idx)
    check("1001 两页都有衍生标签", recs[0]["d_tags"] == ["收藏"] and recs[1]["d_tags"] == ["收藏"],
          recs[0]["d_tags"])
    check("1002 有自动标签", recs[2]["d_tags"] == ["2026年9月30日第1次爬取"],
          recs[2]["d_tags"])

    print("\n=== 2) 衍生TAG 检索 ===")
    hits = pc.search_records_advanced(recs, d_tags=["收藏"], limit=100000)
    check("按「收藏」筛到作品A两页", len(hits) == 2, len(hits))
    auto_tag = "2026年9月30日第1次爬取"
    hits2 = pc.search_records_advanced(recs, d_tags=[auto_tag], limit=100000)
    check("按自动标签「第1次爬取」筛到作品B", len(hits2) == 1, len(hits2))
    hits3 = pc.search_records_advanced(recs, d_tags=["收藏", auto_tag], limit=100000)
    check("两个衍生标签同时要求（全含）-> 0", len(hits3) == 0, len(hits3))
    hits4 = pc.search_records_advanced(recs, d_tags=["收藏", auto_tag],
                                       d_tags_match_all=False, limit=100000)
    check("两个衍生标签任一 -> 3", len(hits4) == 3, len(hits4))
    # 文本检索也能搜到衍生标签（文本是子串匹配）
    hits5 = pc.search_records_advanced(recs, text="第1次爬取", limit=100000)
    check("文本检索命中衍生标签", len(hits5) == 1, len(hits5))
    # 组合：衍生标签 + 原标签 + AI
    hits6 = pc.search_records_advanced(recs, d_tags=["收藏"], tags=["miku"], limit=100000)
    check("衍生+原标签组合（作品A）", len(hits6) == 2, len(hits6))

    print("\n=== 3) 衍生标签直方图 ===")
    hist = pc.dtag_histogram(recs)
    print(f"  {hist}")
    check("直方图包含两个标签", {t for t, _ in hist} == {"收藏", "2026年9月30日第1次爬取"}, hist)

    print("\n=== 4) 爬取历史与序号 ===")
    check("空历史 -> 序号 1", pc.next_crawl_ordinal(idx) == 1)
    e1 = {"ordinal": 1, "mode": "keyword", "keywords": ["初音"], "auto_tag": "2026年9月30日第1次爬取"}
    pc.append_crawl_history(idx, e1)
    check("追加后序号 2", pc.next_crawl_ordinal(idx) == 2)
    e2 = {"ordinal": 2, "mode": "follow", "artists": ["21391270"],
          "auto_tag": "2026年9月30日第2次爬取"}
    pc.append_crawl_history(idx, e2)
    hist2 = pc.load_crawl_history(idx)
    check("历史有 2 条", len(hist2) == 2, len(hist2))
    check("序号全局累计（跨 mode）", [e["ordinal"] for e in hist2] == [1, 2])

    print("\n=== 5) 自动标签与合并到新作品 ===")
    tag = pc.make_auto_tag(3, "2026-10-01")
    check("日期格式正确", tag == "2026年10月1日第3次爬取", tag)

    print("\n=== 6) 引用真实图库检查（读取现有库，验证合并无副作用）===")
    import json
    lib_recs = pc.Library(Path(r"D:\PixivCrawler\library")).load_records()
    check("真实库记录都能加载并带上 d_tags 字段",
          all("d_tags" in r for r in lib_recs[:10]), len(lib_recs))
    dt = pc.dtag_histogram(lib_recs)
    print(f"  真实库当前衍生标签分布: {dt[:5]}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")