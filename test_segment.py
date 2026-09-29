# -*- coding: utf-8 -*-
"""直接验证 crawl_segmented：用一个小时间段真跑，看分段、去重、断点续爬是否都对。"""
import importlib.util
import sys
from datetime import date
from pathlib import Path

spec = importlib.util.spec_from_file_location("pc", r"D:\PixivCrawler\pixiv_crawler.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

fails = []


def check(name, ok, extra=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
    if not ok:
        fails.append(name)


cfg, _ = pc.load_config(Path(r"D:\PixivCrawler"), None)
cfg["requests_per_second"] = 1.0
lib = pc.Library(Path(cfg["output_dir"]))
# 先清掉段状态，保证这个测试可以重复运行：
# 否则上一次跑留下的"已完成"段会让第 1 步直接跳过，后面的断言随之错位（实测踩到）
_stale = lib.index_dir / "segments.json"
if _stale.is_file():
    _stale.unlink()
    print(f"已清理上次的段状态：{_stale}")
sess = pc.CrawlSession(cfg, lib, verbose=True)
sess.known = {}

# 挑一个很小的历史时间段（2007 年 9 月 pixiv 刚开站，作品极少），
# 这样能在一两分钟内完整走完"细分 -> 取完 -> 标记完成"的全流程
START, END = date(2007, 9, 1), date(2007, 9, 30)
print(f"\n测试时间段：{START} ~ {END}（pixiv 开站初期，作品少）")
print(f"目标每段 < {pc.SEG_TARGET} 条\n")

stats = sess.crawl_segmented("初音ミク", start=START, end=END, target=pc.SEG_TARGET)

print("\n=== 1) 分段统计 ===")
for k in ("segments_done", "segments_split", "segments_skipped", "segments_truncated", "pages", "works"):
    print(f"  {k:<20} {stats.get(k)}")
check("有段完成", stats["segments_done"] >= 1, stats["segments_done"])
check("收集到作品", stats["works"] >= 1, stats["works"])
check("没有截断段（该时段很冷）", stats["segments_truncated"] == 0, stats["segments_truncated"])

print("\n=== 2) 收集到的作品都落在时间段内 ===")
out_of_range = 0
dates = []
for it in sess._seg_collected:
    d = pc.parse_illust_datetime(it.get("createDate"))
    if d is None:
        continue
    dd = d.date()
    dates.append(dd)
    if not (START <= dd <= END):
        out_of_range += 1
check("全部落在 2007-09 内", out_of_range == 0, f"越界 {out_of_range} 个")
if dates:
    print(f"  日期范围 {min(dates)} ~ {max(dates)}，共 {len(dates)} 个")
check("ID 无重复", len({str(x.get('id')) for x in sess._seg_collected}) == len(sess._seg_collected))

print("\n=== 3) 段状态已落盘（断点续爬）===")
seg_file = lib.index_dir / "segments.json"
check("segments.json 已生成", seg_file.is_file(), seg_file)
store2 = pc.SegmentStore(seg_file)
print(f"  记录 {len(store2.done)} 段已完成")
check("重新加载后段数一致", len(store2.done) == stats["segments_done"],
      f"{len(store2.done)} vs {stats['segments_done']}")

print("\n=== 4) 再跑一次：应全部跳过（不再重复请求）===")
before_requests = sess.http.stats["requests"]
sess2 = pc.CrawlSession(cfg, lib, verbose=True)
sess2.known = {}
stats2 = sess2.crawl_segmented("初音ミク", start=START, end=END, target=pc.SEG_TARGET)
used = sess2.http.stats["requests"] - before_requests
print(f"  第二次运行：完成 {stats2['segments_done']} 段，跳过 {stats2['segments_skipped']} 段，"
      f"发请求 {used} 次")
check("第二次全部跳过", stats2["segments_done"] == 0 and stats2["segments_skipped"] >= 1,
      f"done={stats2['segments_done']} skipped={stats2['segments_skipped']}")
check("第二次几乎不发请求", used <= 2, used)

print("\n=== 5) 细分行为（用一个热门月份看是否会分）===")
sess3 = pc.CrawlSession(cfg, lib, verbose=True)
sess3.known = {}
s3 = sess3.crawl_segmented("初音ミク", start=date(2026, 9, 1), end=date(2026, 9, 3),
                           target=300, pages_per_seg=10)
print(f"  3 天、目标 300 条/段：细分 {s3['segments_split']} 次，完成 {s3['segments_done']} 段，"
      f"截断 {s3['segments_truncated']} 段，收集 {s3['works']} 个")
check("目标很小时会发生细分", s3["segments_split"] >= 1, s3["segments_split"])

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
sys.exit(0 if not fails else 1)
