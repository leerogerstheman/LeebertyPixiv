#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser_cookie.py —— 从本机浏览器 Cookie 库读取 pixiv 的 PHPSESSID

这是一个**纯函数模块**（不提供命令行入口）：用户入口统一在
`pixiv_crawler.py auth login`，它会按需调用这里的函数。

为什么需要单独的模块：Chrome / Edge 把 cookie 用 AES-256-GCM 加密，密钥再由
Windows DPAPI 保护。本模块用**纯标准库**（ctypes 调 DPAPI + 自实现 AES-GCM）
解密，因此整个项目依然零第三方依赖。

⚠️ 现实限制（实测结论，Edge 154 / Windows）
    Edge 127+ / Chrome 127+ 对 cookie 启用了 v20「应用绑定加密」
    （Local State 里的 app_bound_encrypted_key），其密钥受 **SYSTEM 级 DPAPI**
    保护，普通用户权限无法解开 —— 这是浏览器有意为之的防护。
    实测本机 Edge 的 17 条 pixiv cookie 全部是 v20，因此**自动读取会失败**。
    程序对此的处理是：如实报告原因，并引导用户改用
        · pixiv_crawler.py auth login --method token        （OAuth，推荐）
        · pixiv_crawler.py auth login --method cookie       （手动粘贴 PHPSESSID）
    Firefox 的 cookie 是明文 sqlite，不经这套加密，通常可以直接读到。

自检：python browser_cookie.py
    用 FIPS-197 与官方 GCM 测试向量验证内置的 AES-GCM 实现是否正确。
"""

from __future__ import annotations

import base64
import ctypes
import glob
import os
import sqlite3
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DOMAIN_SUFFIX = "pixiv.net"

# --------------------------------------------------------------------------------------
# 纯 Python AES-256-GCM（状态一律按列主序：state[col][row] 对应输入字节 4*col+row）
# --------------------------------------------------------------------------------------

_SBOX = [
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
]
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D]


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    res = 0
    while b:
        if b & 1:
            res ^= a
        a = _xtime(a)
        b >>= 1
    return res & 0xFF


def _expand_key(key: bytes) -> List[List[int]]:
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(nr + 1)]


def _encrypt_block(rk: List[List[int]], block: bytes) -> bytes:
    """AES 单块加密。状态按列主序存放：state[col][row] 对应输入字节 4*col+row。"""
    nr = len(rk) - 1
    state = [list(block[4 * c:4 * c + 4]) for c in range(4)]

    def add_round_key(rnd: int) -> None:
        for c in range(4):
            for r in range(4):
                state[c][r] ^= rk[rnd][4 * c + r]

    def sub_bytes() -> None:
        for c in range(4):
            for r in range(4):
                state[c][r] = _SBOX[state[c][r]]

    def shift_rows() -> None:
        for r in range(1, 4):
            row = [state[c][r] for c in range(4)]
            row = row[r:] + row[:r]
            for c in range(4):
                state[c][r] = row[c]

    def mix_columns() -> None:
        for c in range(4):
            a = state[c][:]
            state[c][0] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
            state[c][1] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
            state[c][2] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
            state[c][3] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)

    add_round_key(0)
    for rnd in range(1, nr):
        sub_bytes()
        shift_rows()
        mix_columns()
        add_round_key(rnd)
    sub_bytes()
    shift_rows()
    add_round_key(nr)
    return bytes(state[c][r] for c in range(4) for r in range(4))


def _ghash_mul(x: int, y: int) -> int:
    z, v = 0, y
    for i in range(127, -1, -1):
        if (x >> i) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ 0xE1000000000000000000000000000000
        else:
            v >>= 1
    return z


def _ghash(h: bytes, data: bytes) -> bytes:
    y = 0
    hi = int.from_bytes(h, "big")
    for i in range(0, len(data), 16):
        blk = data[i:i + 16].ljust(16, b"\x00")
        y = _ghash_mul(y ^ int.from_bytes(blk, "big"), hi)
    return y.to_bytes(16, "big")


def aes_gcm_decrypt(key: bytes, blob: bytes) -> bytes:
    """解密 Chrome/Edge 的 v10/v11 cookie：nonce(12) + 密文 + tag(16)，无 AAD。

    注意：GCM 的认证是对**密文**做的（S = GHASH(A || C || len)）。
    曾经写成对明文做校验，导致多块数据时误拒合法凭据 —— 已修正。
    """
    nonce, body = blob[:12], blob[12:]
    ct, tag = body[:-16], body[-16:]
    rk = _expand_key(key)
    h = _encrypt_block(rk, b"\x00" * 16)
    j0 = nonce + b"\x00\x00\x00\x01"
    ctr = int.from_bytes(j0, "big")
    out = bytearray()
    for i in range(0, len(ct), 16):
        ctr = (ctr & ~0xFFFFFFFF) | ((ctr + 1) & 0xFFFFFFFF)
        ks = _encrypt_block(rk, ctr.to_bytes(16, "big"))
        out += bytes(a ^ b for a, b in zip(ct[i:i + 16], ks))
    lengths = (0).to_bytes(8, "big") + (len(ct) * 8).to_bytes(8, "big")
    expect = _ghash(h, b"" + ct + lengths)
    expect = bytes(a ^ b for a, b in zip(expect, _encrypt_block(rk, j0)))
    if expect != tag:
        raise ValueError("AES-GCM 校验失败（可能是 v20 加密或密钥不匹配）")
    return bytes(out)


# --------------------------------------------------------------------------------------
# Windows DPAPI
# --------------------------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(data: bytes) -> bytes:
    blob_in = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data),
                                          ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError(f"DPAPI 解密失败（err={ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# --------------------------------------------------------------------------------------
# 浏览器定位
# --------------------------------------------------------------------------------------

BROWSERS: List[Tuple[str, str]] = [
    ("Edge", r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
    ("Chrome", r"%LOCALAPPDATA%\Google\Chrome\User Data"),
    ("Chrome Beta", r"%LOCALAPPDATA%\Google\Chrome Beta\User Data"),
    ("Brave", r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data"),
    ("Vivaldi", r"%LOCALAPPDATA%\Vivaldi\User Data"),
    ("Chromium", r"%LOCALAPPDATA%\Chromium\User Data"),
    ("360极速浏览器", r"%LOCALAPPDATA%\360Chrome\Chrome\User Data"),
    ("QQ浏览器", r"%LOCALAPPDATA%\Tencent\QQBrowser\User Data"),
]
COOKIE_SUBPATHS = [r"Default\Network\Cookies", r"Profile *\Network\Cookies",
                   r"*Profile*\Network\Cookies"]
BROWSER_PROCESSES = {
    "msedge.exe": "Edge", "chrome.exe": "Chrome", "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi", "firefox.exe": "Firefox", "chromium.exe": "Chromium",
    "360chrome.exe": "360极速浏览器", "qqbrowser.exe": "QQ浏览器",
}


def find_chromium_profiles() -> List[Tuple[str, Path]]:
    found: List[Tuple[str, Path]] = []
    for name, rel in BROWSERS:
        base = Path(os.path.expandvars(rel))
        if not base.is_dir():
            continue
        for sub in COOKIE_SUBPATHS:
            for p in glob.glob(str(base / sub)):
                if os.path.isfile(p) and (name, Path(p)) not in found:
                    found.append((name, Path(p)))
    return found


def find_firefox_profiles() -> List[Tuple[str, Path]]:
    found: List[Tuple[str, Path]] = []
    base = Path(os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles"))
    if base.is_dir():
        for p in glob.glob(str(base / "*" / "cookies.sqlite")):
            found.append(("Firefox", Path(p)))
    return found


def running_processes() -> Dict[str, int]:
    procs: Dict[str, int] = {}
    try:
        import subprocess

        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, timeout=20, encoding="utf-8", errors="replace").stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                name = parts[0].strip('"').lower()
                procs[name] = procs.get(name, 0) + 1
    except Exception:  # noqa: BLE001
        pass
    return procs


def running_browsers() -> List[str]:
    procs = running_processes()
    return sorted({label for exe, label in BROWSER_PROCESSES.items() if exe in procs})


# --------------------------------------------------------------------------------------
# 读取与解密
# --------------------------------------------------------------------------------------

def chromium_master_key(cookies_path: Path) -> Optional[bytes]:
    """从同一 profile 的 Local State 取出 v10 主密钥（DPAPI 保护）。"""
    root = cookies_path
    for _ in range(3):
        root = root.parent
        local_state = root / "Local State"
        if local_state.is_file():
            try:
                import json

                data = json.loads(local_state.read_text(encoding="utf-8", errors="replace"))
                enc = base64.b64decode(data["os_crypt"]["encrypted_key"])
                if enc[:5] == b"DPAPI":
                    enc = enc[5:]
                return dpapi_unprotect(enc)
            except Exception:  # noqa: BLE001
                return None
    return None


def decrypt_cookie_value(raw: bytes, key: Optional[bytes]) -> str:
    if not raw:
        return ""
    if raw[:3] in (b"v10", b"v11"):
        if key is None:
            raise ValueError("缺少 v10 主密钥")
        return aes_gcm_decrypt(key, raw[3:]).decode("utf-8", "replace")
    if raw[:3] == b"v20":
        raise ValueError("v20 应用绑定加密（Chrome/Edge 127+），密钥受 SYSTEM 级 DPAPI 保护，"
                         "普通权限无法解密")
    return dpapi_unprotect(raw).decode("utf-8", "replace")


def _copy_locked_db(db: Path) -> Tuple[Optional[Path], Optional[str]]:
    """Cookie 库被占用时复制一份再读（含兼容共享读的备份语义打开）。"""
    import shutil

    tmp_dir = Path(tempfile.mkdtemp(prefix="pixiv_cookie_"))
    copied = False
    for suffix in ("", "-wal", "-shm", "-journal"):
        src = Path(str(db) + suffix)
        if src.is_file():
            try:
                shutil.copy2(src, tmp_dir / (db.name + suffix))
                copied = copied or suffix == ""
            except OSError:
                pass
    copy = tmp_dir / db.name
    if not copied or not copy.is_file():
        shutil.rmtree(tmp_dir, ignore_errors=True)
        names = running_browsers()
        hint = f"检测到正在运行：{'、'.join(names)}。" if names else ""
        return None, (f"Cookie 库被浏览器独占，无法读取。{hint}"
                      "请完全退出该浏览器（含托盘残留进程）后重试。")
    return copy, None


def read_entries(browser: str, db: Path) -> List[Dict[str, str]]:
    """读取该 Cookie 库里所有 pixiv.net 的 cookie（含解密结果或错误原因）。"""
    is_firefox = browser == "Firefox"
    key = None if is_firefox else chromium_master_key(db)
    tmp_copy: Optional[Path] = None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        copy, err = _copy_locked_db(db)
        if copy is None:
            raise RuntimeError(err or "无法读取 Cookie 库")
        tmp_copy = copy
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True, timeout=5)

    out: List[Dict[str, str]] = []
    try:
        if is_firefox:
            rows = conn.execute("select host, name, value from moz_cookies where host like ?",
                                (f"%{DOMAIN_SUFFIX}%",)).fetchall()
            for host, name, value in rows:
                out.append({"host": host, "name": name, "value": value or "", "error": ""})
        else:
            rows = conn.execute(
                "select host_key, name, encrypted_value from cookies where host_key like ?",
                (f"%{DOMAIN_SUFFIX}%",)).fetchall()
            for host, name, enc in rows:
                try:
                    value, err = decrypt_cookie_value(bytes(enc or b""), key), ""
                except Exception as exc:  # noqa: BLE001
                    value, err = "", str(exc)
                out.append({"host": host, "name": name, "value": value, "error": err})
    finally:
        conn.close()
        if tmp_copy is not None:
            import shutil

            shutil.rmtree(tmp_copy.parent, ignore_errors=True)
    return out


# --------------------------------------------------------------------------------------
# 自检：用公开测试向量验证 AES-GCM 实现（不涉及任何真实密钥）
# --------------------------------------------------------------------------------------

def _selftest() -> int:
    import os as _os

    fails: List[str] = []

    def check(name: str, ok: bool, extra: str = "") -> None:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + extra) if extra else ''}")
        if not ok:
            fails.append(name)

    print("AES / GCM 实现自检（公开测试向量）")
    k = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    check("FIPS-197 C.1 AES-128",
          _encrypt_block(_expand_key(k), pt).hex() == "69c4e0d86a7b0430d8cdb78070b4c55a")
    k256 = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
    check("FIPS-197 C.3 AES-256",
          _encrypt_block(_expand_key(k256), pt).hex() == "8ea2b7ca516745bfeafc49904b496089")
    rk = _expand_key(bytes(16))
    check("GCM H = E(K,0)", _encrypt_block(rk, bytes(16)).hex() ==
          "66e94bd4ef8a2c3b884cfa59ca342b2e")
    try:
        out = aes_gcm_decrypt(bytes(16), bytes(12) + b"" +
                              bytes.fromhex("58e2fccefa7e3061367f1d57a4e7455a"))
        check("GCM 官方向量 1（空消息）", out == b"")
    except ValueError as exc:
        check("GCM 官方向量 1（空消息）", False, str(exc))
    try:
        out = aes_gcm_decrypt(bytes(16), bytes(12) +
                              bytes.fromhex("0388dace60b6a392f328c2b971b2fe78") +
                              bytes.fromhex("ab6e47d42cec13bdf53a67b21257bddf"))
        check("GCM 官方向量 2（16 字节明文）", out == bytes(16))
    except ValueError as exc:
        check("GCM 官方向量 2（16 字节明文）", False, str(exc))

    # 往返 + 篡改拒绝 + 错误密钥拒绝
    key, nonce = _os.urandom(32), _os.urandom(12)
    plain = b"1234567890abcdef1234567890abcdef"
    rk = _expand_key(key)
    h = _encrypt_block(rk, bytes(16))
    j0 = int.from_bytes(nonce + b"\x00\x00\x00\x01", "big")
    ct = bytearray()
    for i in range(0, len(plain), 16):
        j0 = (j0 & ~0xFFFFFFFF) | ((j0 + 1) & 0xFFFFFFFF)
        ks = _encrypt_block(rk, j0.to_bytes(16, "big"))
        ct += bytes(a ^ b for a, b in zip(plain[i:i + 16], ks))
    tag = _ghash(h, bytes(ct) + (0).to_bytes(8, "big") + (len(ct) * 8).to_bytes(8, "big"))
    tag = bytes(a ^ b for a, b in zip(tag, _encrypt_block(rk, nonce + b"\x00\x00\x00\x01")))
    blob = b"v10" + nonce + bytes(ct) + tag
    try:
        check("v10 往返解密", decrypt_cookie_value(blob, key) == plain.decode())
    except Exception as exc:  # noqa: BLE001
        check("v10 往返解密", False, f"{type(exc).__name__}: {exc}")
    bad = bytearray(blob)
    bad[20] ^= 0x01
    try:
        decrypt_cookie_value(bytes(bad), key)
        check("篡改应被拒绝", False, "竟然通过了")
    except ValueError:
        check("篡改应被拒绝", True)
    try:
        decrypt_cookie_value(blob, _os.urandom(32))
        check("错误密钥应被拒绝", False, "竟然通过了")
    except ValueError:
        check("错误密钥应被拒绝", True)

    print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    if os.name != "nt":
        print("本模块的浏览器 cookie 读取依赖 Windows DPAPI。")
        print("在其它系统上请改用：python pixiv_crawler.py auth login --method cookie")
        sys.exit(0)
    sys.exit(_selftest())
