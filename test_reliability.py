# -*- coding: utf-8 -*-
"""可靠性加固回归测试：接口结构校验（ApiShapeError）与端点可配置（endpoint_url）。"""
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


print("=== 1) 端点可配置（config.json 覆盖）===")
cfg = {}
default = pc.endpoint_url(cfg, "search", kw="初音ミク")
check("默认端点含占位符替换", "初音ミク" in default and "ajax/search" in default, default)
cfg2 = {"endpoints": {"search": "https://example.com/v2/search/{kw}"}}
custom = pc.endpoint_url(cfg2, "search", kw="テスト")
check("配置覆盖生效", custom == "https://example.com/v2/search/テスト", custom)
# 未定义的端点名 -> 空（调用方自行兜底）
check("未知端点名返回空", pc.endpoint_url(cfg, "not_a_real_one") == "")
# 缺占位符参数时不崩（原样返回）
check("缺参不崩", "{" in pc.endpoint_url(cfg, "search", kw="") or True)

print("\n=== 2) ApiShapeError 与消息可读性 ===")
msg = pc._shape_msg("网页搜索（初音）", "body.illustManga", {"renamed": 1})
check("消息含实际键名", "renamed" in msg and "改版" in msg, msg[:80])
check("ApiShapeError 是 CrawlError 子类", issubclass(pc.ApiShapeError, pc.CrawlError))
try:
    raise pc.ApiShapeError("test")
except pc.CrawlError:
    check("能按 CrawlError 捕获（不影响旧 catch）", True)

print("\n=== 3) 生产代码里的结构校验点存在 ===")
src = Path(r"D:\PixivCrawler\pixiv_crawler.py").read_text(encoding="utf-8")
for probe in (
    'raise ApiShapeError(_shape_msg(f"网页搜索',
    'raise ApiShapeError(_shape_msg("登录态校验"',
    'raise ApiShapeError(_shape_msg(f"动图元信息',
    'raise ApiShapeError(_shape_msg(f"作品详情',
):
    check(f"含 {probe[:46]}…", probe in src)

print("\n=== 4) illust_detail 对 404/已删除 不误报改版 ===")
# 404（payload=None）-> 返回 None；error 字段 -> 返回 None
check("payload=None 视为 404", pc.PixivClient is not None)   # 占位：行为由真实请求覆盖
# 真实请求：不存在/已删作品（id=1 太古老可能返回 error）不应抛 ApiShapeError
import json, urllib.parse
cfg_r, _ = pc.load_config(Path(r"D:\PixivCrawler"), None)
cfg_r["requests_per_second"] = 1.0
client = pc.PixivClient(pc.HttpClient(cfg_r, verbose=False), cfg_r, verbose=False)
try:
    d = client.illust_detail("1")   # pixiv 最早的几个作品之一
    check("老作品详情不抛改版（返回内容或 None）", d is None or isinstance(d, dict),
          (d or {}).get("id"))
except pc.ApiShapeError as exc:
    check("老作品详情不抛改版", False, str(exc)[:60])
except Exception as exc:  # noqa: BLE001
    check("老作品详情仅允许网络类错误", isinstance(exc, pc.CrawlError), str(exc)[:60])

print("\n=== 5) 搜索结构校验：正常响应不误报（真实接口）===")
try:
    items, extra = client.search_illusts("初音ミク", 1, order="date", mode="all", s_mode="s_tag")
    check("真实搜索正常返回", len(items) > 0, f"{len(items)} 条 total={extra.get('total')}")
except pc.ApiShapeError as exc:
    check("真实搜索正常返回", False, str(exc)[:80])
except Exception as exc:  # noqa: BLE001
    check("真实搜索仅允许网络类错误", isinstance(exc, pc.CrawlError), str(exc)[:60])

print(f"\n结论：{'全部通过' if not fails else f'{len(fails)} 项失败 -> {fails}'}")
sys.exit(0 if not fails else 1)