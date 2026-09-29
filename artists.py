#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
artists.py —— 画师订阅清单（追更模式用）

## 为什么需要"追更模式"

关键词搜索是"一次性"的：今天搜「初音ミク」拿到一批，明天再搜还是那一批的更新版本，
你要的是"我喜欢的这几个画师，出了新图就自动收下来"。
这就是多数同类工具最常用的场景（PixivUtil2 的"画师订阅"、PixivBiu 的"关注追更"）。

## 清单格式

纯文本 `artists.txt`，一行一个画师，用 `|` 分隔，方便手改也方便程序解析：

    画师ID | 画师名 | 上次追更时间 | 备注

例：
    73260619|四宮いずな|2026-09-28T13:20:00+08:00|
    21391270|らいおん||很喜欢的画师

* 画师ID 必填；其余可空
* 以 `#` 开头的行是注释，空行忽略
* 用 `pip` 的 URL 或主页链接粘贴进来也行，`parse_artist_ref()` 会解析出 ID
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ARTISTS_FILENAME = "artists.txt"

# 支持从这些形式里抠出画师 ID
_ID_PATTERNS = [
    re.compile(r"/users/(\d+)"),
    re.compile(r"/member\.php\?id=(\d+)"),
    re.compile(r"/member_illust\.php\?id=(\d+)"),
    re.compile(r"[?&]id=(\d+)"),
    re.compile(r"^(\d{3,})$"),
]


def parse_artist_ref(text: str) -> Optional[str]:
    """从 URL / 纯 ID / 各种链接里解析出画师 ID。"""
    s = (text or "").strip()
    if not s:
        return None
    for pat in _ID_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    return None


@dataclass
class Artist:
    artist_id: str
    name: str = ""
    last_sync: str = ""
    note: str = ""
    added_at: str = field(default="")

    def to_line(self) -> str:
        return "|".join([self.artist_id, self.name, self.last_sync, self.note])

    def label(self) -> str:
        return f"{self.name}（id={self.artist_id}）" if self.name else f"id={self.artist_id}"


class ArtistStore:
    """artists.txt 的读写。故意用纯文本：便于用户直接编辑、也便于 git 管理。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.artists: List[Artist] = []
        self.load()

    # ---- 读写 ----

    def load(self) -> None:
        self.artists = []
        if not self.path.is_file():
            return
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except OSError:
            return
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            aid = parse_artist_ref(parts[0]) if parts else None
            if not aid:
                continue
            self.artists.append(Artist(
                artist_id=aid,
                name=parts[1] if len(parts) > 1 else "",
                last_sync=parts[2] if len(parts) > 2 else "",
                note=parts[3] if len(parts) > 3 else "",
            ))

    def save(self) -> None:
        lines = [
            "# pixiv 追更清单 —— 一行一个画师，格式：",
            "#   画师ID|画师名|上次追更时间|备注",
            "# 用 follow add 命令添加，或直接编辑本文件。以 # 开头的是注释。",
            "",
        ]
        for a in self.artists:
            lines.append(a.to_line())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # ---- 操作 ----

    def get(self, artist_id: str) -> Optional[Artist]:
        for a in self.artists:
            if a.artist_id == artist_id:
                return a
        return None

    def add(self, artist_id: str, name: str = "", note: str = "") -> tuple[Artist, bool]:
        """返回 (画师, 是否新增)。已存在时只补全空缺的名字/备注。"""
        exist = self.get(artist_id)
        if exist:
            changed = False
            if name and not exist.name:
                exist.name = name
                changed = True
            if note and not exist.note:
                exist.note = note
                changed = True
            return exist, changed
        a = Artist(artist_id=artist_id, name=name, note=note,
                   added_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        self.artists.append(a)
        return a, True

    def remove(self, artist_id: str) -> bool:
        before = len(self.artists)
        self.artists = [a for a in self.artists if a.artist_id != artist_id]
        return len(self.artists) != before

    def mark_synced(self, artist_id: str, when: Optional[str] = None) -> None:
        a = self.get(artist_id)
        if a:
            a.last_sync = when or datetime.now().astimezone().isoformat(timespec="seconds")

    def summary(self) -> Dict[str, Any]:
        return {
            "count": len(self.artists),
            "named": sum(1 for a in self.artists if a.name),
            "synced": sum(1 for a in self.artists if a.last_sync),
        }


# --------------------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------------------

def _selftest() -> int:
    import tempfile
    import shutil

    fails: List[str] = []

    def check(name: str, ok: bool, extra: Any = "") -> None:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + str(extra)) if extra != '' else ''}")
        if not ok:
            fails.append(name)

    print("画师订阅清单自检")
    print("\n=== 1) 链接/ID 解析 ===")
    cases = [
        ("https://www.pixiv.net/users/73260619", "73260619"),
        ("https://www.pixiv.net/en/users/73260619", "73260619"),
        ("https://www.pixiv.net/users/73260619/artworks", "73260619"),
        ("https://www.pixiv.net/member.php?id=12345", "12345"),
        ("https://www.pixiv.net/member_illust.php?id=12345", "12345"),
        ("73260619", "73260619"),
        ("  73260619  ", "73260619"),
    ]
    for raw, want in cases:
        got = parse_artist_ref(raw)
        check(f"{raw[:46]!r}", got == want, f"got={got!r}")
    for bad in ("", "   ", "四宮いずな", "12", "abc"):
        check(f"拒绝 {bad!r}", parse_artist_ref(bad) is None)

    tmp = Path(tempfile.mkdtemp(prefix="artists_test_"))
    try:
        p = tmp / ARTISTS_FILENAME
        print("\n=== 2) 清单读写 ===")
        store = ArtistStore(p)
        check("空清单可加载", store.artists == [])
        a, is_new = store.add("73260619", name="四宮いずな")
        check("新增画师", is_new and a.name == "四宮いずな")
        store.add("21391270", name="らいおん", note="动图作者")
        store.save()
        check("文件已写出", p.is_file())

        store2 = ArtistStore(p)
        check("重新加载条数正确", len(store2.artists) == 2, len(store2.artists))
        check("名字/备注保留", store2.get("21391270").note == "动图作者")
        check("artists.py 自己写的头注释被忽略",
              all(not a.name.startswith("#") for a in store2.artists))

        print("\n=== 3) 重复添加与容错 ===")
        _, is_new2 = store2.add("73260619", name="改个名字")
        check("重复添加不新增", not is_new2)
        check("已有名字不被覆盖", store2.get("73260619").name == "四宮いずな")
        before = len(store2.artists)
        store2.add("73260619")
        check("重复添加不增加条数", len(store2.artists) == before)

        p.write_text("# 注释行\n\n99999|测试\n乱码行没有ID\n12345|B\n", encoding="utf-8")
        store3 = ArtistStore(p)
        check("忽略注释与无法识别的行", len(store3.artists) == 2, [x.artist_id for x in store3.artists])

        print("\n=== 4) 移除与时间戳 ===")
        check("移除存在的画师", store3.remove("99999"))
        check("移除不存在的画师返回 False", not store3.remove("00000"))
        store3.add("55555", name="X")
        store3.mark_synced("55555", "2026-01-02T03:04:05+08:00")
        check("mark_synced 生效", store3.get("55555").last_sync.startswith("2026-01-02"))
        check("summary 统计正确",
              store3.summary()["count"] == len(store3.artists))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_selftest())
