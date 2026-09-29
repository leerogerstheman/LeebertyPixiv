# -*- coding: utf-8 -*-
"""验证"按画师 ID 检索"在两处都可用：爬取页输入框、图库检索页输入框。"""
import argparse
import importlib.util
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


def flush(n=8):
    for _ in range(n):
        root.update_idletasks(); root.update(); root.after(1, lambda: None); root.update()


def check(name, ok, extra=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
    if not ok:
        fails.append(name)


def find(w, cls, out):
    for c in w.winfo_children():
        if isinstance(c, cls):
            out.append(c)
        find(c, cls, out)


flush()
nbs = []
find(root, tk.ttk.Notebook, nbs)
nb = nbs[0]

print("=== 1) 爬取页有「画师ID」输入框 ===")
tab0 = nb.nametowidget(nb.tabs()[0])
nb.select(nb.tabs()[0]); flush()
labels = []
find(tab0, tk.ttk.Label, labels)
lt = [str(l.cget("text")) for l in labels]
print(f"  标签中含画师ID：{[t for t in lt if '画师' in t]}")
check("爬取页有「画师ID：」标签", any("画师ID" in t for t in lt), [t for t in lt if "画师" in t])
entries = []
find(tab0, tk.ttk.Entry, entries)
check("爬取页输入框数量合理", len(entries) >= 2, len(entries))
# 找到绑在画师输入框上的变量
aid_entry = None
for e in entries:
    try:
        vn = str(e.cget("textvariable"))
        if vn:
            aid_entry = e
    except Exception:  # noqa: BLE001
        pass
print(f"  输入框：{[str(e.cget('textvariable')) for e in entries]}")
check("爬取页有至少 2 个输入框（关键词 + 画师ID）", len(entries) >= 2, len(entries))

print("\n=== 2) 图库检索页有「画师ID」输入框 ===")
tab1 = nb.nametowidget(nb.tabs()[1])
nb.select(nb.tabs()[2]); flush()
tab_lib2 = nb.nametowidget(nb.tabs()[2])
labels2 = []
find(tab_lib2, tk.ttk.Label, labels2)
lt2 = [str(l.cget("text")) for l in labels2]
print(f"  标签中含画师ID：{[t for t in lt2 if '画师' in t]}")
check("检索页有「画师ID：」标签", any("画师ID" in t for t in lt2), [t for t in lt2 if "画师" in t])

print("\n=== 3) 检索页填画师 ID 能真的筛出来 ===")
entries2 = []
find(tab_lib2, tk.ttk.Entry, entries2)
# 画师ID 输入框是宽度 18 的那个
aid_entry2 = None
for e in entries2:
    try:
        if int(e.cget("width")) == 18:
            aid_entry2 = e
            break
    except Exception:  # noqa: BLE001
        pass
check("找到画师ID输入框（宽度18）", aid_entry2 is not None,
      [str(e.cget("width")) for e in entries2])
trees = []
find(tab_lib2, tk.ttk.Treeview, trees)
tree = trees[0]
# 切到检索页后等初始检索完成（run_search 是经 event loop 异步触发的，直接量会是 0）
for _ in range(40):
    flush(3)
    if len(tree.get_children()) > 0:
        break
before = len(tree.get_children())
print(f"  未填画师时的结果数：{before}")
if aid_entry2 is not None:
    aid_entry2.delete(0, "end")
    aid_entry2.insert(0, "5961223")
    btns = []
    find(root, tk.ttk.Button, btns)
    next(b for b in btns if b.cget("text") == "检索").invoke()
    flush()
    after = len(tree.get_children())
    print(f"  填了画师 5961223 后的结果数：{after}")
    check("结果减少", after < before, f"{before} -> {after}")
    check("结果非空", after > 0, after)
    if after:
        vals = [tree.item(i, "values") for i in tree.get_children()[:5]]
        authors = {str(v[2]) for v in vals}
        print(f"  前几行画师：{authors}")
    # 主页链接形式
    aid_entry2.delete(0, "end")
    aid_entry2.insert(0, "https://www.pixiv.net/users/5961223")
    next(b for b in btns if b.cget("text") == "检索").invoke()
    flush()
    after2 = len(tree.get_children())
    print(f"  用主页链接形式：{after2}")
    check("链接形式结果一致", after2 == after, f"{after2} vs {after}")

print("\n=== 4) 重置会清空画师 ID ===")
btns = []
find(root, tk.ttk.Button, btns)
next(b for b in btns if b.cget("text") == "重置").invoke()
flush()
check("重置后画师ID清空", aid_entry2.get() == "", aid_entry2.get())
check("重置后结果恢复", len(tree.get_children()) == before,
      f"{len(tree.get_children())} vs {before}")

root.destroy()
print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
