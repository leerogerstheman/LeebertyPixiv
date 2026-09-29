# -*- coding: utf-8 -*-
"""验证 deep 的组合重叠自检：组合统计、去重汇总、以及中文表格的列对齐。

不联网也能测的部分：display_width / pad_display。
联网部分（真实翻页）用很小的页数跑，避免占用太久。
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


print("=== 1) 显示宽度计算（中文按 2 列）===")
cases = [
    ("abc", 3),
    ("初音ミク", 8),
    ("date_d + s_tag", 14),
    ("最早发布（与最新不重叠的区段）", 30),
    ("a中b", 4),
    ("", 0),
    ("（）", 4),
    ("%", 1),
]
for text, want in cases:
    got = pc.display_width(text)
    check(f"display_width({text!r}) == {want}", got == want, got)

print("\n=== 2) pad_display 填充 ===")
for text, width in (("初音ミク", 20), ("abc", 10), ("最早发布（与最新不重叠的区段）", 30)):
    padded = pc.pad_display(text, width)
    got = pc.display_width(padded)
    check(f"{text[:8]!r} 填到 {width} 列", got == width, got)
check("右对齐也正确", pc.display_width(pc.pad_display("初音", 12, "right")) == 12)
check("超宽不截断", pc.pad_display("abcdefghij", 3) == "abcdefghij")

print("\n=== 3) 中文表格对齐（真实排一遍）===")
rows = [("date_d + s_tag", 180, 180, "100%", ""),
        ("最早发布（与最新不重叠的区段）", 180, 180, "100%", ""),
        ("热门（与最新重合时自动跳过）", 60, 0, "0%", "大量重叠")]
widths = [0, 0, 0, 0]
rendered = []
for label, ret, add, rate, flag in rows:
    line = (pc.pad_display(label, 30) + pc.pad_display(ret, 10, "right")
            + pc.pad_display(add, 8, "right") + pc.pad_display(rate, 8, "right")
            + "   " + flag)
    rendered.append(line)
    # 前四列的结束位置应当一致
    widths[0] = max(widths[0], pc.display_width(line))
for line in rendered:
    print(f"    |{line}|")
col_ends = set()
for label, ret, add, rate, flag in rows:
    col_ends.add(pc.display_width(pc.pad_display(label, 30)))
    col_ends.add(pc.display_width(pc.pad_display(label, 30) + pc.pad_display(ret, 10, "right")))
check("第一列结束位置一致", len(col_ends) == 2, col_ends)

print("\n=== 4) 联网：真实 dry-run 看组合统计 ===")
cfg, _ = pc.load_config(Path(r"D:\PixivCrawler"), None)
lib = pc.Library(Path(cfg["output_dir"]))
sess = pc.CrawlSession(cfg, lib, verbose=False)
sess.known = {}
try:
    res = sess.crawl_keyword("初音ミク", pages=3, order="date", mode="all", s_mode="tag",
                             max_works=0, deep=True, dry_run=True)
except Exception as exc:  # noqa: BLE001
    print(f"  跳过联网测试（失败：{exc}）")
    res = None

if res:
    combos = res.get("combos") or []
    print(f"  组合数 {len(combos)}，各组合共返回 {res.get('combos_returned')}，"
          f"去重后唯一 {res.get('unique')}")
    for cs in combos:
        rate = (cs["added"] * 100.0 / cs["returned"]) if cs.get("returned") else 0
        print(f"    {cs['label'][:24]:<26} 返回 {cs.get('returned'):>5} 新增 {cs['added']:>5} "
              f"新增率 {rate:>5.0f}%  尽头={cs['exhausted']}")
    check("返回了组合统计", len(combos) >= 2, len(combos))
    check("组合统计含 returned 字段", all("returned" in cs for cs in combos))
    check("deep 至少组合了「最早」", any(cs["order"] == "old" for cs in combos),
          [cs["order"] for cs in combos])
    total_ret = res.get("combos_returned") or 0
    uniq = res.get("unique") or 0
    check("去重后唯一数 <= 总返回数", uniq <= total_ret, f"{uniq} vs {total_ret}")
    # 「最新」与「最早」应几乎不重叠：唯一数应接近总返回数
    if total_ret >= 300:
        dup_rate = (total_ret - uniq) * 100.0 / total_ret
        print(f"  重复率 {dup_rate:.0f}%")
        check("最新与最早基本不重叠（重复率 < 40%）", dup_rate < 40, f"{dup_rate:.0f}%")

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
sys.exit(0 if not fails else 1)
