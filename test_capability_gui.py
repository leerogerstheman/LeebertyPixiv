# -*- coding: utf-8 -*-
"""读取能力页的端到端测试：切到该页 -> 自动体检 -> 验证渲染出的结论与按钮可用。"""
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
                          min_likes=None, min_bookmarks=None)
holder = {}
tk.Tk.mainloop = lambda self, *a, **k: holder.update(root=self)
pc.cmd_gui(args, Path(r"D:\PixivCrawler"))
root = holder["root"]

fails = []


def pump(seconds=0.0):
    end = time.time() + seconds
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


def texts():
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


pump(1.0)
nbs = []
find(root, tk.ttk.Notebook, nbs)
nb = nbs[0]
print("=== 1) 标签页结构 ===")
titles = [nb.tab(t, "text").strip() for t in nb.tabs()]
print(f"  {titles}")
check("四个平级标签页", titles == ["爬取", "追更", "图库检索", "图库位置", "读取能力", "爬取历史"], titles)

print("\n=== 2) 切到「读取能力」触发自动体检 ===")
nb.select(nb.tabs()[4])
nb.event_generate("<<NotebookTabChanged>>")
pump(1.0)
banner = [t for t in texts() if any(k in t for k in ("正在检测", "完全可用", "仅匿名", "部分受限", "检测失败"))]
print(f"  横幅: {banner}")
check("自动开始检测", any("正在检测" in t for t in banner), banner)

print("\n=== 3) 等体检跑完（最多 240 秒）===")
deadline = time.time() + 240
done = False
while time.time() < deadline:
    pump(1.0)
    cur = [t for t in texts() if any(k in t for k in ("完全可用", "仅匿名", "部分受限", "检测失败"))]
    if cur:
        print(f"  结论横幅: {cur[0]}")
        done = True
        break
check("体检完成并给出结论", done)

print("\n=== 4) 四个探测项都渲染了结果 ===")
allt = texts()
for key in ("① 登录凭据", "② 登录态", "③ 可读取量（登录 vs 匿名）", "④ 原图下载"):
    row = [t for t in allt if t.startswith(("✓ ", "✗ ", "· "))]
    print(f"  {key} 存在: {key in allt}")
    check(f"{key} 行存在", key in allt)
rows = [t for t in allt if t.startswith(("✓ ", "✗ ", "· "))]
print("  探测结果行:")
for r in rows:
    print(f"    {r[:100]}")
check("至少 3 项有结论", len(rows) >= 3, len(rows))
check("没有停留在「检测中…」", not any("检测中" in r for r in rows), rows)

print("\n=== 5) 明细信息（可读取量）===")
cap = [t for t in allt if "登录可读" in t]
print(f"  {cap}")
check("显示了登录/匿名对比", bool(cap), cap)

print("\n=== 6) 建议区 ===")
adv = [t for t in allt if "一切正常" in t or "重新导入" in t or "（无）" in t]
print(f"  {adv}")
check("给出了建议", bool(adv), adv)

print("\n=== 7) 按钮可用 ===")
btns = []
# 从「读取能力」页（tab 4）内找，其它标签页的按钮不相关
find(nb.nametowidget(nb.tabs()[4]), tk.ttk.Button, btns)
for name in ("开始检测", "从浏览器自动导入 Cookie", "手动填 PHPSESSID…", "用 pixiv 官网登录（OAuth）",
             "打开凭据库目录", "清除已保存凭据"):
    b = next((x for x in btns if x.cget("text") == name), None)
    check(f"按钮「{name}」存在且可点", b is not None and "disabled" not in b.state(),
          b.state() if b else "缺失")

print("\n=== 8) 凭据优先级说明里有真实路径 ===")
prio = [t for t in allt if "凭据库" in t and "credentials.json" in t]
texts_w = []
find(root, tk.Text, texts_w)
for t in texts_w:
    content = t.get("1.0", "end")
    if "凭据优先级" in content or "credentials.json" in content:
        print("  优先级文本:")
        for line in content.strip().splitlines():
            print(f"    {line}")
        check("含凭据库路径", "credentials.json" in content)
        check("含 config.json 路径", "config.json" in content)
        break

root.destroy()
print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
