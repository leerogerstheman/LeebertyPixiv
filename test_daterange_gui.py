# -*- coding: utf-8 -*-
"""验证两个标签页的三级下拉：联动、取值、与检索联动。

注意：日期下拉的变量由 build_date_range 内部创建，测试必须通过它返回的
`vars` 句柄操作，否则改的是"另一个同名变量"、界面根本不会响应。
"""
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
                          min_likes=None, min_bookmarks=None)
holder = {}
tk.Tk.mainloop = lambda self, *a, **k: holder.update(root=self)
pc.cmd_gui(args, Path(r"D:\PixivCrawler"))
root = holder["root"]

fails = []


def flush(n=10):
    for _ in range(n):
        root.update_idletasks()
        root.update()
        root.after(1, lambda: None)
        root.update()


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

# 直接取 GUI 建好的两个 date_range：从模块级闭包里拿不到，改用控件反查
# —— 通过"父容器里同时含 从 和 到 标签"精确定位两组下拉，各取 6 个
nbs = []
find(root, tk.ttk.Notebook, nbs)
nb = nbs[0]
tabs = nb.tabs()


def date_groups(tab_index):
    """返回该标签页里的"日期组"（每组恰好 6 个下拉）。

    注意：只认**恰好含 6 个 Combobox** 的那个 frame。外层容器里也有「从」「到」两个
    标签，天真的判断会把同一组数两次（实测踩到），所以这里按数量精确识别。
    """
    cbs = []
    find(nb.nametowidget(tabs[tab_index]), tk.ttk.Combobox, cbs)
    groups = []
    seen = set()
    for c in cbs:
        parent = c.master
        if parent in seen:
            continue
        kids = [x for x in parent.winfo_children() if isinstance(x, tk.ttk.Combobox)]
        if len(kids) == 6:
            seen.add(parent)
            groups.append(kids)
    return groups


print("=== 1) 两页各有一组日期下拉（且都已布局）===")
for idx, name in ((0, "爬取"), (1, "图库检索")):
    nb.select(tabs[idx]); flush()
    groups = date_groups(idx)
    print(f"  [{name}] 日期组 {len(groups)} 个，每组 {[len(g) for g in groups]} 个下拉")
    check(f"{name}页恰好 1 组", len(groups) == 1, len(groups))
    if groups:
        widths = [c.winfo_width() for c in groups[0]]
        print(f"     宽度={widths}")
        check(f"{name}页 6 个下拉", len(groups[0]) == 6, len(groups[0]))
        check(f"{name}页都已布局", all(w > 20 for w in widths), widths)

print("\n=== 2) 检索页联动（用真实控件）===")
nb.select(tabs[1]); flush()
grp = date_groups(1)[0]
yf_cb, mf_cb, df_cb, yt_cb, mt_cb, dt_cb = grp
(yf, mf, df), (yt, mt, dt) = (yf_cb.cget("textvariable"), mf_cb.cget("textvariable"),
                              df_cb.cget("textvariable")), (yt_cb.cget("textvariable"),
                              mt_cb.cget("textvariable"), dt_cb.cget("textvariable"))
# 用控件的 textvariable 名字拿到真实的 Tcl 变量并操作
tcl = root.tk

check("初始月/日禁用", "disabled" in mf_cb.state() and "disabled" in df_cb.state())
tcl.setvar(yf, "2024"); flush()
check("选年后月可用", "disabled" not in mf_cb.state())
check("选年后日仍禁用", "disabled" in df_cb.state())
tcl.setvar(mf, "2"); flush()
check("选月后日可用", "disabled" not in df_cb.state())
check("2024-02 = 29 天", len(df_cb.cget("values")) - 1 == 29, len(df_cb.cget("values")) - 1)
tcl.setvar(yf, "2023"); flush()
check("2023-02 = 28 天", len(df_cb.cget("values")) - 1 == 28, len(df_cb.cget("values")) - 1)

print("\n=== 3) 与检索联动：限定年份后结果应减少 ===")
trees = []
find(root, tk.ttk.Treeview, trees)
tree = trees[0]
for v in (yf, mf, df, yt, mt, dt):
    tcl.setvar(v, "不限")
flush()
baseline = len(tree.get_children())
print(f"  不限时间：{baseline} 行")
check("不限时间有结果", baseline > 0, baseline)

tcl.setvar(yf, "2026"); tcl.setvar(yt, "2026")
flush()
after = len(tree.get_children())
print(f"  限定 2026 年：{after} 行")
check("结果减少（或等于且年份都正确）", after <= baseline, f"{baseline} -> {after}")
years = {str(tree.item(i, "values")[7])[:4] for i in tree.get_children()}
print(f"  结果年份集合: {sorted(years)}")
check("结果年份都在 2026", years in ({"2026"}, set()), sorted(years))

print("\n=== 4) 限定到很老的年份应显著减少 ===")
tcl.setvar(yf, "2007"); tcl.setvar(yt, "2007")
flush()
old = len(tree.get_children())
years_old = {str(tree.item(i, "values")[7])[:4] for i in tree.get_children()}
print(f"  限定 2007 年：{old} 行，年份集合={sorted(years_old)}")
check("明显减少", old < after, f"{after} -> {old}")
check("年份都在 2007", years_old in ({"2007"}, set()), sorted(years_old))

print("\n=== 5) 重置恢复 ===")
btns = []
find(root, tk.ttk.Button, btns)
next(b for b in btns if b.cget("text") == "重置").invoke()
flush()
back = len(tree.get_children())
print(f"  重置后：{back} 行")
check("恢复全部", back == baseline, f"{back} vs {baseline}")
check("日期清空", all(tcl.getvar(v) == "不限" for v in (yf, mf, df, yt, mt, dt)))

print("\n=== 6) 两页互不影响 ===")
nb.select(tabs[0]); flush()
grp0 = date_groups(0)[0]
tcl.setvar(grp0[0].cget("textvariable"), "2007")
tcl.setvar(grp0[1].cget("textvariable"), "9")
flush()
check("爬取页已设为 2007-9",
      tcl.getvar(grp0[0].cget("textvariable")) == "2007"
      and tcl.getvar(grp0[1].cget("textvariable")) == "9")
nb.select(tabs[1]); flush()
check("检索页仍是「不限」",
      all(tcl.getvar(v) == "不限" for v in (yf, mf, df, yt, mt, dt)))

root.destroy()
print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
