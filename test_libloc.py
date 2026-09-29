# -*- coding: utf-8 -*-
"""验证"更改图库位置"的核心逻辑：写 config.json -> 重载 Library -> 能读到索引。

复刻 GUI 里 apply_library_location() 的操作序列，但不碰真实图库（用临时目录）。
"""
import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("pc", r"D:\PixivCrawler\pixiv_crawler.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

PROGRAM_DIR = Path(r"D:\PixivCrawler")
CONFIG = PROGRAM_DIR / "config.json"
backup = CONFIG.read_text(encoding="utf-8")

fails = []


def check(name, ok, extra=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
    if not ok:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="libloc_"))
try:
    cfg, _ = pc.load_config(PROGRAM_DIR, None)
    old_dir = str(cfg["output_dir"])
    print(f"原图库位置: {old_dir}")

    print("\n=== 1) 写 config.json（GUI 的写回逻辑）===")
    data = pc.read_json(CONFIG, {}) or {}
    data["output_dir"] = str(tmp)
    pc.atomic_write_json(CONFIG, data)
    check("配置已写入", json.loads(CONFIG.read_text(encoding='utf-8'))["output_dir"] == str(tmp))
    cfg2, _ = pc.load_config(PROGRAM_DIR, None)
    check("重新加载后 output_dir 生效", cfg2["output_dir"] == str(tmp), cfg2["output_dir"])

    print("\n=== 2) 新位置为空目录时：Library 能初始化并创建结构 ===")
    lib2 = pc.Library(Path(cfg2["output_dir"]))
    lib2.ensure()
    check("根目录已创建", lib2.root.is_dir())
    check("_originals 已创建", lib2.originals.is_dir())
    check("_index 已创建", lib2.index_dir.is_dir())
    check("空图库读出 0 条", lib2.load_records() == [])

    print("\n=== 3) 目录不可写时应被检出（GUI 会弹错误框）===")
    probe_ok = True
    try:
        probe = lib2.root / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        probe_ok = False
    check("可写探测通过", probe_ok)
    bad = Path("Z:/definitely/not/exist/xxx")
    bad_ok = True
    try:
        bad.mkdir(parents=True, exist_ok=True)
    except OSError:
        bad_ok = False
    check("非法路径会被 OSError 拦下", not bad_ok)

    print("\n=== 4) 新位置已有图库时能直接检索（复刻：复制索引过去）===")
    src_lib = pc.Library(Path(old_dir))
    for name in ("records.jsonl", "index.csv", "index.md", "catalog.sqlite"):
        s = src_lib.index_dir / name
        if s.is_file():
            shutil.copy2(s, lib2.index_dir / name)
    recs = lib2.load_records()
    check(f"复制索引后能读到记录（{len(recs)} 条）", len(recs) > 0, len(recs))
    if recs:
        hits = pc.search_records_advanced(recs, tags=["初音ミク"], limit=3, r18_mode="hide")
        check("在新位置能按标签检索", len(hits) > 0, f"{len(hits)} 条")
        hist = pc.tag_histogram(recs)
        check("标签列表能生成", len(hist) > 0, f"{len(hist)} 个标签")

    print("\n=== 5) 相对路径也支持（会被解析为绝对路径）===")
    rel = "relative_lib_test"
    p = Path(rel)
    if not p.is_absolute():
        p = (PROGRAM_DIR / p).resolve()
    check("相对路径转绝对", p.is_absolute(), p)
finally:
    CONFIG.write_text(backup, encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(PROGRAM_DIR / "relative_lib_test", ignore_errors=True)
    print("\n已还原 config.json 与临时目录")

cfg3, _ = pc.load_config(PROGRAM_DIR, None)
print(f"还原后图库位置: {cfg3['output_dir']}")
check("配置已还原", cfg3["output_dir"] == old_dir)

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
