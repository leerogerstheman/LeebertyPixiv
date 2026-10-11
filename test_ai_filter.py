# -*- coding: utf-8 -*-
"""AIGC 过滤语义回归测试。

背景（真实 bug，2026-10 修复）：
    pixiv 的 aiType 语义是  0=未标注 / 1=非AI（人类）/ 2=AI生成。
    早期版本用 `aiType != 0` 当作"是 AI"，于是把「1=人类」也当成 AI，导致：
        · 选「排除 AI」→ 实际只留下 AI 作品（完全反了）
        · 选「只要 AI」→ 人类+AI 全留（等于没筛）
    本测试把正确语义钉死，防止以后再写反。

不联网：用构造数据验证判断函数与两层过滤（图库检索 / 爬取列表阶段）。
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


print("=== 1) is_ai_work 语义（0/1=非AI，2=AI）===")
for val, want in ((0, False), (1, False), (2, True), (3, True), (None, False),
                  ("0", False), ("1", False), ("2", True), ("", False)):
    check(f"is_ai_work({val!r}) == {want}", pc.is_ai_work(val) == want, pc.is_ai_work(val))

print("\n=== 2) 图库检索的 AI 筛选（记录里 ai 字段 = 原始 aiType）===")
recs = [
    {"id": "1", "ai": 1, "x_restrict": 0, "tags": ["x"], "title": "人类作品"},
    {"id": "2", "ai": 2, "x_restrict": 0, "tags": ["x"], "title": "AI作品"},
    {"id": "3", "ai": 1, "x_restrict": 0, "tags": ["x"], "title": "人类作品2"},
    {"id": "4", "ai": 0, "x_restrict": 0, "tags": ["x"], "title": "未标注"},
]
all_hits = pc.search_records_advanced(recs, ai_mode="all", limit=0)
ex_hits = pc.search_records_advanced(recs, ai_mode="exclude", limit=0)
on_hits = pc.search_records_advanced(recs, ai_mode="only", limit=0)
check("不限 -> 全部 4 条", len(all_hits) == 4, len(all_hits))
check("排除 AI -> 只剩人类（3 条：ai=1 与 ai=0）", len(ex_hits) == 3, [r["id"] for r in ex_hits])
check("排除 AI 结果里没有 ai=2", all(r["id"] != "2" for r in ex_hits))
check("只要 AI -> 只剩 ai=2（1 条）", len(on_hits) == 1 and on_hits[0]["id"] == "2",
      [r["id"] for r in on_hits])

print("\n=== 3) 爬取列表阶段过滤（模拟 CrawlSession 的判断）===")
briefs = [
    {"id": "a", "aiType": 1},   # 人类
    {"id": "b", "aiType": 2},   # AI
    {"id": "c", "aiType": 1},   # 人类
]


def filter_briefs(ai_only: bool, ai_exclude: bool):
    out = []
    for b in briefs:
        is_ai = pc.is_ai_work(b.get("aiType"))
        if (ai_only and not is_ai) or (ai_exclude and is_ai):
            continue
        out.append(b["id"])
    return out


check("exclude 模式 -> ['a','c']（人类）", filter_briefs(False, True) == ["a", "c"],
      filter_briefs(False, True))
check("only 模式 -> ['b']（AI）", filter_briefs(True, False) == ["b"], filter_briefs(True, False))
check("不限 -> ['a','b','c']", filter_briefs(False, False) == ["a", "b", "c"])

print("\n=== 4) 真实语义快照（防再次写反的对照）===")
print("  0 = 未标注   -> 不算 AI")
print("  1 = 非AI人类 -> 不算 AI  ← 关键：曾经被误判为 AI")
print("  2 = AI 生成  -> 算 AI")
check("aiType=1 绝不能被判为 AI", pc.is_ai_work(1) is False)

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
sys.exit(0 if not fails else 1)