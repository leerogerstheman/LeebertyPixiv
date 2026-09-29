# -*- coding: utf-8 -*-
"""防回归：标签页顺序与「读取能力」自动体检的触发。

背景：加「追更」标签页后，读取能力从 tab 3 变成 tab 4，
但 `nb.bind(<<NotebookTabChanged>>, ...)` 里的判断仍是 ==3，
导致切到读取能力页时不再自动体检 —— 一个真实回归，靠 GUI 测试抓到。
这里固化下来，防止以后再改标签顺序时踩同款坑。
"""
import argparse
import importlib.util
import time
import tkinter as tk
from pathlib import Path

spec = importlib.util.spec_from_file_location("pc", r"D:\PixivCrawler\pixiv_crawler.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

args = argparse.Namespace(config=None, out=None, cookie=None, refresh_token=None, proxy=None,
                          insecure=False, concurrency=None, rps=None, timeout=None,
                          keep_r18=False, keep_r18g=False, keep_ugoira=False,
                          ugoira_format=None, drop_ugoira_zip=False, verbose=False,
                          max_pages_per_work=None, date_from=None, date_to=None,
                          min_likes=None, min_bookmarks=None, mode=None, s_mode=None,
                          order=None, pages=None, limit=None, deep=False, deep_all=False,
                          dry_run=False, restart=False, force=False, ai_mode=None)
holder = {}
tk.Tk.mainloop = lambda self, *a, **k: holder.update(root=self)
pc.cmd_gui(args, Path(r"D:\PixivCrawler"))
root = holder["root"]

fails = []


def pump(sec=1.0):
    end = time.time() + sec
    while True:
        root.update_idletasks(); root.update(); root.after(1, lambda: None); root.update()
        if time.time() >= end:
            break
        time.sleep(0.05)


def check(name, ok, extra=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
    if not ok:
        fails.append(name)


def find(w, cls, out):
    for c in w.winfo_children():
        if isinstance(c, cls):
            out.append(c)
        find(c, cls, out)


def all_texts():
    out = []
    labs = []
    find(root, tk.ttk.Label, labs)
    for l in labs:
        try:
            t = str(l.cget("text"))
        except Exception:  # noqa: BLE001
            continue
        if t:
            out.append(t)
    return out


pump(2)
nbs = []
find(root, tk.ttk.Notebook, nbs)
nb = nbs[0]

print("=== 1) 标签页顺序（追更必须在第 2 位）===")
titles = [nb.tab(t, "text").strip() for t in nb.tabs()]
print(f"  {titles}")
check("六页且顺序正确", titles == ["爬取", "追更", "图库检索", "图库位置", "读取能力", "爬取历史"],
      titles)

print("\n=== 2) 切到「读取能力」应自动启动体检 ===")
nb.select(nb.tabs()[4])
nb.event_generate("<<NotebookTabChanged>>")
pump(3)
banners = [t for t in all_texts() if any(k in t for k in
          ("正在检测", "完全可用", "仅匿名", "部分受限", "检测失败", "正在实测", "正在校验"))]
print(f"  横幅: {banners[:2]}")
check("自动体检已启动", any(("正在检测" in t) or ("正在实测" in t) for t in banners), banners[:2])

print("\n=== 3) 追更页可加载清单（构造后立即有表）===")
tab1 = nb.nametowidget(nb.tabs()[1])
nb.select(nb.tabs()[1])
pump(1)
tvs = []
find(tab1, tk.ttk.Treeview, tvs)
check("追更页有清单表格", len(tvs) == 1, len(tvs))

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")