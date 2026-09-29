# -*- coding: utf-8 -*-
"""验证库检索的筛选维度：标签多选、点赞/收藏、时间、R-18 分级、排序。"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("pc", r"D:\PixivCrawler\pixiv_crawler.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

cfg, _ = pc.load_config(Path(r"D:\PixivCrawler"), None)
lib = pc.Library(Path(cfg["output_dir"]))
recs = lib.load_records()
print(f"图库共 {len(recs)} 条记录\n")

fails = []


def check(name, ok, extra=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
    if not ok:
        fails.append(name)


print("=== 1) 标签直方图 ===")
hist = pc.tag_histogram(recs)
print(f"  共 {len(hist)} 个标签，前 5: {hist[:5]}")
check("直方图非空且降序", bool(hist) and all(hist[i][1] >= hist[i+1][1] for i in range(min(5, len(hist)-1))))
top_tag = hist[0][0] if hist else ""

print("\n=== 2) 标签筛选：全部命中 vs 任一命中 ===")
two = [t for t, _ in hist[:2]]
all_hits = pc.search_records_advanced(recs, tags=two, tags_match_all=True)
any_hits = pc.search_records_advanced(recs, tags=two, tags_match_all=False)
print(f"  标签 {two} 全含 = {len(all_hits)} 条；任一 = {len(any_hits)} 条")
check("全含 <= 任一", len(all_hits) <= len(any_hits))
check("单标签全含结果合理", len(pc.search_records_advanced(recs, tags=[top_tag])) > 0)

print("\n=== 3) R-18 分级 ===")
for mode in ("hide", "no_g", "only", "all"):
    n = len(pc.search_records_advanced(recs, r18_mode=mode))
    print(f"  r18_mode={mode:<5} -> {n} 条")
check("hide <= all", len(pc.search_records_advanced(recs, r18_mode="hide")) <=
      len(pc.search_records_advanced(recs, r18_mode="all")))
check("分级字段存在（x_restrict）", all("x_restrict" in r for r in recs[:5]))

print("\n=== 4) 点赞 / 收藏门槛 ===")
for th in (0, 10, 100, 1000):
    n = len(pc.search_records_advanced(recs, min_likes=th))
    print(f"  点赞 >= {th:<5} -> {n} 条")
check("门槛提高结果不增加",
      len(pc.search_records_advanced(recs, min_likes=100)) <=
      len(pc.search_records_advanced(recs, min_likes=10)))
for th in (0, 50, 500):
    n = len(pc.search_records_advanced(recs, min_bookmarks=th))
    print(f"  收藏 >= {th:<5} -> {n} 条")

print("\n=== 5) 发布时间范围 ===")
for df, dt in (("", ""), ("2026", "2026"), ("2007", "2007"), ("2020", "2022")):
    n = len(pc.search_records_advanced(recs, date_from=df, date_to=dt))
    print(f"  {df or '不限':<5} ~ {dt or '不限':<5} -> {n} 条")
check("2026 年结果 > 0", len(pc.search_records_advanced(recs, date_from="2026", date_to="2026")) > 0)

print("\n=== 6) 排序 ===")
by_likes = pc.search_records_advanced(recs, sort_by="likes", limit=5)
by_bm = pc.search_records_advanced(recs, sort_by="bookmarks", limit=5)
by_date = pc.search_records_advanced(recs, sort_by="date", limit=5)
by_size = pc.search_records_advanced(recs, sort_by="size", limit=5)
print("  点赞 Top5:", [pc.as_int(r.get("like_count"), 0) for r in by_likes])
print("  收藏 Top5:", [pc.as_int(r.get("bookmark_count"), 0) for r in by_bm])
print("  日期 Top5:", [str(r.get("create_date"))[:10] for r in by_date])
print("  大小 Top5(MB):", [round(pc.as_int(r.get("bytes"), 0)/1048576, 1) for r in by_size])
check("点赞降序", all(pc.as_int(by_likes[i].get("like_count"), 0) >=
                     pc.as_int(by_likes[i+1].get("like_count"), 0) for i in range(len(by_likes)-1)))
check("收藏降序", all(pc.as_int(by_bm[i].get("bookmark_count"), 0) >=
                     pc.as_int(by_bm[i+1].get("bookmark_count"), 0) for i in range(len(by_bm)-1)))
check("日期降序", all(str(by_date[i].get("create_date")) >= str(by_date[i+1].get("create_date"))
                     for i in range(len(by_date)-1)))
check("大小降序", all(pc.as_int(by_size[i].get("bytes"), 0) >=
                     pc.as_int(by_size[i+1].get("bytes"), 0) for i in range(len(by_size)-1)))

print("\n=== 7) 组合筛选 ===")
combo = pc.search_records_advanced(recs, tags=[top_tag], min_likes=1, r18_mode="hide",
                                   date_from="2020", sort_by="likes", limit=10)
print(f"  标签「{top_tag}」+ 点赞>=1 + 全年龄 + 2020年后 -> {len(combo)} 条")
check("组合筛选可执行", isinstance(combo, list))
check("结果都满足点赞门槛", all(pc.as_int(r.get("like_count"), 0) >= 1 for r in combo))
check("结果都全年龄", all(pc.as_int(r.get("x_restrict"), 0) == 0 for r in combo))
check("结果都含该标签", all(top_tag in (r.get("tags") or []) for r in combo))

print("\n=== 8) 上限与文本检索 ===")
check("limit 生效", len(pc.search_records_advanced(recs, limit=3)) <= 3)
txt = pc.search_records_advanced(recs, text="ミク", limit=5)
print(f"  文本「ミク」-> {len(txt)} 条（限 5）")
check("文本检索有结果", len(txt) > 0)

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
