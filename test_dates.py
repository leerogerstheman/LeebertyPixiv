# -*- coding: utf-8 -*-
"""验证时间范围解析与边界逻辑（纯离线，不发请求）。"""
import importlib.util
from datetime import datetime, timedelta

spec = importlib.util.spec_from_file_location("pc", r"D:\PixivCrawler\pixiv_crawler.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

LOCAL = pc._LOCAL_TZ
fails = []


def check(name, ok, extra=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -> ' + extra) if extra else ''}")
    if not ok:
        fails.append(name)


print("=== 1) parse_user_date 各种格式 ===")
cases = {
    "2024-01-31": datetime(2024, 1, 31, tzinfo=LOCAL),
    "2024/01/31": datetime(2024, 1, 31, tzinfo=LOCAL),
    "2024.01.31": datetime(2024, 1, 31, tzinfo=LOCAL),
    "20240131": datetime(2024, 1, 31, tzinfo=LOCAL),
    "2024-01-31 18:30": datetime(2024, 1, 31, 18, 30, tzinfo=LOCAL),
    "2024年1月31日": datetime(2024, 1, 31, tzinfo=LOCAL),
    "": None,
    "   ": None,
    None: None,
}
for text, want in cases.items():
    got = pc.parse_user_date(text)
    check(f"解析 {text!r}", got == want, f"got={got}")

print("\n=== 2) 非法格式应报错而不是静默 ===")
for bad in ("去年", "2024-13-45", "abc"):
    try:
        got = pc.parse_user_date(bad)
        check(f"拒绝 {bad!r}", False, f"竟然返回 {got}")
    except ValueError:
        check(f"拒绝 {bad!r} 并提示", True)

print("\n=== 2b) 只给年月/只给年（界面下拉用的新格式）应被接受 ===")
for text, want in (("2024-1", datetime(2024, 1, 1, tzinfo=LOCAL)),
                   ("2024-12", datetime(2024, 12, 1, tzinfo=LOCAL)),
                   ("2024-07", datetime(2024, 7, 1, tzinfo=LOCAL)),
                   ("2024", datetime(2024, 1, 1, tzinfo=LOCAL))):
    got = pc.parse_user_date(text, mode="start")
    check(f"接受 {text!r}", got == want, f"got={got}")
check("结束侧 2024 -> 整年（次年 1 月 1 日）",
      pc.parse_user_date("2024", mode="end") == datetime(2025, 1, 1, tzinfo=LOCAL))
check("结束侧 2024-07 -> 整月（次月 1 日）",
      pc.parse_user_date("2024-07", mode="end") == datetime(2024, 8, 1, tzinfo=LOCAL))

print("\n=== 3) 结束日期包含当天（关键语义）===")
rng = pc.parse_range({"date_from": "2024-01-01", "date_to": "2024-01-31"})
check("范围描述", rng.describe().startswith("2024-01-01 00:00"), rng.describe())
for dt, want, label in (
    (datetime(2024, 1, 1, 0, 0, tzinfo=LOCAL), True, "起始当天 00:00 应收"),
    (datetime(2023, 12, 31, 23, 59, tzinfo=LOCAL), False, "起始前一天应排除"),
    (datetime(2024, 1, 31, 23, 59, 59, tzinfo=LOCAL), True, "结束当天 23:59:59 应收（含当天）"),
    (datetime(2024, 2, 1, 0, 0, tzinfo=LOCAL), False, "结束次日应排除"),
    (datetime(2024, 1, 15, 12, 0, tzinfo=LOCAL), True, "范围中间应收"),
):
    check(label, rng.contains(dt) == want)

print("\n=== 4) 只填一端 ===")
r1 = pc.parse_range({"date_from": "2024-06-01"})
check("只填起始：更早的排除", not r1.contains(datetime(2024, 5, 31, tzinfo=LOCAL)))
check("只填起始：之后的收", r1.contains(datetime(2030, 1, 1, tzinfo=LOCAL)))
r2 = pc.parse_range({"date_to": "2024-06-30"})
check("只填结束：之后的排除", not r2.contains(datetime(2024, 7, 1, tzinfo=LOCAL)))
check("只填结束：之前的收", r2.contains(datetime(2000, 1, 1, tzinfo=LOCAL)))

print("\n=== 5) 留空 = 不限制 ===")
r3 = pc.parse_range({})
check("未配置时不激活", not r3.active)
check("未配置时全都收", r3.contains(datetime(1990, 1, 1, tzinfo=LOCAL)) and r3.contains(None))

print("\n=== 6) 起止填反要给出警告并忽略 ===")
r4 = pc.parse_range({"date_from": "2024-12-31", "date_to": "2024-01-01"})
check("起晚于止 -> 不激活", not r4.active)

print("\n=== 7) parse_illust_datetime（web +09:00 / app +00:00）===")
jst = pc.parse_illust_datetime("2026-09-28T14:03:43+09:00")
utc = pc.parse_illust_datetime("2026-09-28T05:03:00+00:00")
check("+09:00 可解析", jst is not None and jst.hour == 14)
check("+00:00 可解析", utc is not None)
# 用真正等价的时刻比较（14:03:43+09:00 == 05:03:43+00:00）
same_a = pc.parse_illust_datetime("2026-09-28T14:03:43+09:00")
same_b = pc.parse_illust_datetime("2026-09-28T05:03:43+00:00")
check("同一时刻两种时区写法相等", same_a == same_b, f"{same_a} vs {same_b}")
check("不同时刻不相等（原测试用例写错了）", jst != utc)
check("时间戳相等（epoch 比较）",
      same_a.timestamp() == same_b.timestamp())
check("空值返回 None", pc.parse_illust_datetime(None) is None)
check("Z 结尾可解析", pc.parse_illust_datetime("2026-09-28T05:03:00Z") == utc)

print("\n=== 8) is_older_than_range（用于提前停止翻页）===")
check("早于起始 -> True", rng.is_older_than_range(datetime(2023, 1, 1, tzinfo=LOCAL)))
check("范围内 -> False", not rng.is_older_than_range(datetime(2024, 1, 15, tzinfo=LOCAL)))
check("未设起始 -> 永远 False", not r3.is_older_than_range(datetime(1990, 1, 1, tzinfo=LOCAL)))

print("\n=== 9) 时区穿越：JST 深夜作品与本地日界 ===")
# 2024-02-01 07:00 JST = 2024-01-31 22:00 UTC；本地若为 UTC+8 则是 2024-02-01 06:00
work = pc.parse_illust_datetime("2024-02-01T07:00:00+09:00")
r_only_jan = pc.parse_range({"date_from": "2024-01-01", "date_to": "2024-01-31"})
local_day = work.astimezone(LOCAL)
print(f"   作品 UTC 时刻 {work.astimezone(pc.timezone.utc)}，本地 {local_day}")
check("按本地日期判断（不是按 JST 原字符串）",
      r_only_jan.contains(work) == (local_day.date() <= datetime(2024, 1, 31, tzinfo=LOCAL).date()))

print("\n=== 10) as_int 容错 ===")
check("None -> 0", pc.as_int(None) == 0)
check("'12' -> 12", pc.as_int("12") == 12)
check("'abc' -> 默认", pc.as_int("abc", -1) == -1)

print("\n结论:", "全部通过" if not fails else f"{len(fails)} 项失败 -> {fails}")
