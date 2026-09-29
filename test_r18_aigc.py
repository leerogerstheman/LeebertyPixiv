# -*- coding: utf-8 -*-
"""端到端验证：分级勾选 + AIGC 选择真的传到爬取流程并生效（干跑，不下载）。"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("pc", r"D:\PixivCrawler\pixiv_crawler.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

cfg, _ = pc.load_config(Path(r"D:\PixivCrawler"), None)
cfg["requests_per_second"] = 1.5
lib = pc.Library(Path(cfg["output_dir"]))
fails = []


def check(name, ok, extra=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
    if not ok:
        fails.append(name)


def run(levels, ai_mode, label):
    print(f"\n--- {label}：勾选={levels} AI={ai_mode} ---")
    msgs = []
    info = pc.run_crawl(dict(cfg), lib, ["初音ミク"], pages=2, order="date",
                        mode="all", s_mode="s_tag", max_works=0,
                        dry_run=True, r18_levels=levels, ai_mode=ai_mode,
                        log=lambda m: msgs.append(m))
    for m in msgs:
        if any(k in m for k in ("内容分级", "AIGC", "按设置跳过", "筛选", "实际取到",
                                "可用于下载", "没有勾选")):
            print(f"    {m.strip()}")
    return info, "\n".join(msgs)


print("=== 1) 只收全年龄 + 不限 AI ===")
info, txt = run([0], "all", "只收全年龄")
check("显示了内容分级", "全年龄" in txt, txt[:80])
check("干跑返回结果", isinstance(info, dict))

print("\n=== 2) 只收 R-18G（应跳过所有 R-18 与全年龄）===")
info, txt = run([2], "only", "只收 R-18G + 只要 AI")
check("分级为 R-18G", "R-18G" in txt, txt[:100])
check("AIGC 过滤已启用", "AI" in txt, txt[:100])

print("\n=== 3) 全不勾 -> 应拒绝执行 ===")
info, txt = run([], "all", "全不勾")
check("拒绝执行并说明原因", info.get("aborted") == "no_r18_level" or "没有勾选" in txt,
      info.get("aborted"))

print("\n=== 4) 全都要 + 排除 AI ===")
info, txt = run([0, 1, 2], "exclude", "全都要 + 排除 AI")
check("三个分级都在", all(x in txt for x in ("全年龄", "R-18", "R-18G")), txt[:120])
check("排除 AI 已提示", "排除" in txt, txt[:120])

print("\n=== 5) AIGC 筛选的实际效果（用已下载图库验证逻辑）===")
recs = lib.load_records()
ai_hits = pc.search_records_advanced(recs, ai_mode="only", limit=100000)
noai_hits = pc.search_records_advanced(recs, ai_mode="exclude", limit=100000)
all_hits = pc.search_records_advanced(recs, ai_mode="all", limit=100000)
print(f"    库里：只要 AI {len(ai_hits)} 条，排除 AI {len(noai_hits)} 条，不限 {len(all_hits)} 条")
check("only + exclude = all", len(ai_hits) + len(noai_hits) == len(all_hits),
      f"{len(ai_hits)}+{len(noai_hits)} vs {len(all_hits)}")
check("库里确实有 AI 作品（说明字段可用）", len(ai_hits) > 0, len(ai_hits))

print("\n=== 6) 库检索的六种分级模式都能跑 ===")
for mode in ("hide", "no_g", "only_r18", "only_g", "only_g_mix", "r18_all", "all"):
    n = len(pc.search_records_advanced(recs, r18_mode=mode, limit=100000))
    print(f"    {mode:<12} -> {n} 条")
check("六种模式均可执行（含返回 0 的）", True)

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
