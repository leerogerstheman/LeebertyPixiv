# -*- coding: utf-8 -*-
"""登录状态下：单排序能翻多深？deep 的组合还能不能带来新增？

这是回答"登录后 deep 是否还有意义"的关键实验：
分别对每种排序尽量往后翻，记录每页新增的唯一作品数，直到连续多页 0 新增。
"""
import importlib.util
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("pc", r"D:\PixivCrawler\pixiv_crawler.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

KW = "初音ミク"
MAX_PAGES = 105          # 每页约 59 个 -> 105 页约 6200 个，足够碰到上限
PATIENCE = 4             # 连续这么多页 0 新增就认为到底了

cfg, _ = pc.load_config(Path(r"D:\PixivCrawler"), None)
http = pc.HttpClient(cfg, verbose=False)
client = pc.PixivClient(http, cfg, verbose=False)
who = client.verify_login()
print(f"登录态：{who.get('name') if who else '（无）'}  "
      f"pixiv_id={who.get('pixiv_id') if who else '-'}  user_id={who.get('user_id') if who else '-'}")
print(f"关键词：{KW}  最大页数：{MAX_PAGES}\n")

orders = [
    ("date",    "最新（默认）"),
    ("old",     "最早"),
    ("popular", "热门"),
]
results = {}
for order, label in orders:
    seen = set()
    total = None
    first_page_ids = None
    zero_streak = 0
    pages_done = 0
    for page in range(1, MAX_PAGES + 1):
        try:
            items, extra = client.search_illusts(KW, page, order=order,
                                                 mode=str(cfg.get("mode") or "all"),
                                                 s_mode="s_tag")
        except pc.CrawlError as exc:
            print(f"  [{order}] 第 {page} 页失败：{exc}")
            break
        if total is None:
            total = extra.get("total")
        if not items:
            print(f"  [{order}] 第 {page} 页起返回空 -> 到底")
            break
        ids = [str(x.get("id")) for x in items]
        if first_page_ids is None:
            first_page_ids = ids
        new = len(set(ids) - seen)
        seen.update(ids)
        pages_done = page
        if new == 0:
            zero_streak += 1
            if zero_streak >= PATIENCE:
                print(f"  [{order}] 第 {page} 页起连续 {PATIENCE} 页 0 新增 -> 停止")
                break
        else:
            zero_streak = 0
        if page % 20 == 0 or page == 1:
            print(f"  [{order}] 已翻 {page} 页，唯一作品 {len(seen)}")
        time.sleep(float(cfg.get("search_delay") or 0.3))
    results[order] = {"ids": seen, "total": total, "pages": pages_done,
                      "first": set(first_page_ids or [])}
    print(f"  => [{order}] {label}：翻了 {pages_done} 页，唯一作品 {len(seen)} 个，"
          f"pixiv 报告总数 {total}\n")

print("=" * 72)
print("各排序独立可得量")
for order, label in orders:
    r = results[order]
    print(f"  {label:<12} {len(r['ids']):>6} 个   翻到第 {r['pages']} 页   pixiv 报告 {r['total']}")

print("\n两两重叠情况（用来判断组合是否还有新增空间）")
names = [o for o, _ in orders]
for i in range(len(names)):
    for j in range(i + 1, len(names)):
        a, b = results[names[i]]["ids"], results[names[j]]["ids"]
        inter = len(a & b)
        union = len(a | b)
        print(f"  {names[i]:<9} ∩ {names[j]:<9} 交集 {inter:>6}  并集 {union:>6}  "
              f"新增（并集-B）{union - len(b):>6}")

all_ids = set()
for o in names:
    all_ids |= results[o]["ids"]
print(f"\n三种排序合并后总唯一作品：{len(all_ids)}")
print(f"  相比只用「最新」({len(results['date']['ids'])}) 增加 "
      f"{len(all_ids) - len(results['date']['ids'])} 个")

# 首屏是否相同（判断 popular 是不是坏的）
print("\n首屏（第 1 页）是否与「最新」相同（判断 popular 排序是否有效）")
d0 = results["date"]["first"]
for o in ("old", "popular"):
    same = results[o]["first"] == d0
    print(f"  {o:<9} 首屏与 date 相同：{same}")
