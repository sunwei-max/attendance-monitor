#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
腾讯会议实时出勤监控 · 可视化后端  meeting_dashboard.py
=====================================================
请求驱动 + 节流：只有前端打开页面并请求 /api/snapshot 时才会去拉腾讯会议数据；
同一轮询周期内（INTERVAL）复用缓存，不会反复调用 API。
=> 平时不开页面 = 零调用、零刷新、零消耗。

运行（演示，无需真实会议）：
  DEMO=1 python3 meeting_dashboard.py
  # 浏览器打开 http://localhost:8080

运行（真实监控）：
  TENCENT_MEETING_TOKEN='你的token' MEETING_CODE=904854736 \
  ROSTER=花名册.xlsx INTERVAL=30 python3 meeting_dashboard.py

环境变量：
  TENCENT_MEETING_TOKEN  腾讯会议 Token（必填，真实模式）
  MEETING_CODE / MEETING_ID  会议号或 meeting_id（二选一，真实模式）
  ROSTER                     花名册路径 csv/xlsx（真实模式）
  INTERVAL                   后端真实拉取的最小间隔秒数（默认30）
  DEMO=1                     演示模式，用内置模拟数据
"""
import os
import time
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import meeting_monitor as mm

TOKEN = os.environ.get("TENCENT_MEETING_TOKEN", "")
MEETING_CODE = os.environ.get("MEETING_CODE")
MEETING_ID = os.environ.get("MEETING_ID")
ROSTER = os.environ.get("ROSTER")
INTERVAL = int(os.environ.get("INTERVAL", "180"))             # 自动刷新节流：默认180秒=3分钟（距上次真实拉取<3分钟复用缓存）
MANUAL_INTERVAL = int(os.environ.get("MANUAL_INTERVAL", "30")) # 手动刷新(🔄按钮)节流：30秒内连点复用缓存
OPEN_INTERVAL = int(os.environ.get("OPEN_INTERVAL", "180"))   # 首屏点开节流：3分钟内重复打开复用缓存，不重复消耗
AUTO_INTERVAL = int(os.environ.get("AUTO_INTERVAL", "1800"))  # 前端自动刷新节奏：默认1800=整点/半点；可设 600(10分)/300(5分)
AUTO_REFRESH = os.environ.get("AUTO_REFRESH", "1") != "0"      # 是否开启自动刷新（默认开；置0则纯手动）
DEMO = os.environ.get("DEMO") == "1"

app = FastAPI()

# 缓存最近一次真实拉取的结果，用于填充展示
cache = {"data": None, "ts": 0.0}
# 自动刷新的独立时间戳：手动刷新不干扰自动节奏，确保整点/半点真正刷新生效
auto_cache = {"ts": 0.0}

# 防耗尽闸门：每日调用上限 + 额度耗尽(500246)后自动锁定，避免无限重试浪费
DAILY_SAFE_LIMIT = 150
AUTO_DISABLE_RATIO = 0.9            # 用到 90% 时，自动刷新自动退场改手动（保留手动刷新），避免无谓消耗
AUTO_DISABLE_THRESHOLD = int(DAILY_SAFE_LIMIT * AUTO_DISABLE_RATIO)  # =135
guard = {"date": None, "count": 0, "exhausted": False}

# 会议号 -> meeting_id 解析结果缓存（重要：避免每次都多消耗一次接口额度）
mid_cache = {"code": None, "mid": None}

# 演示用数据
DEMO_ROSTER = [
    {"name": "张三", "sid": "2027001"},
    {"name": "李四", "sid": "2027002"},
    {"name": "王五", "sid": "2027003"},
    {"name": "赵六", "sid": "2027004"},
    {"name": "孙七", "sid": "2027005"},
]
DEMO_ONLINE = ["张三", "李四", "孙七", "iPhone"]  # 王五/赵六缺席；iPhone 待核对


def parse_iso(s):
    """解析腾讯会议 ISO 时间戳（如 2026-10-03T13:35:54+08:00）。"""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def enrich_present(r, online, join_map):
    """把在线成员的入会时间挂到花名册记录上。"""
    for nk in online:
        if mm.matches(nk, r):
            jt = join_map.get(nk)
            if jt:
                return {**r, "join_time": jt}
            return r
    return r


def make_snapshot(present, absent, unmatched, label, demo=False, meeting_start=None):
    return {
        "total": len(present) + len(absent),
        "present": [{"name": r["name"], "sid": r.get("sid", ""), "join_time": r.get("join_time")} for r in present],
        "absent": [{"name": r["name"], "sid": r.get("sid", "")} for r in absent],
        "unmatched": unmatched,
        "label": label,
        "demo": demo,
        "meeting_start": meeting_start,
        "updated": datetime.now().strftime("%H:%M:%S"),
    }


def resolve_mid():
    """把会议号解析成 meeting_id，并缓存；命中的话不再多消耗一次接口额度。"""
    if MEETING_ID:
        return MEETING_ID
    if mid_cache["code"] == MEETING_CODE and mid_cache["mid"]:
        return mid_cache["mid"]
    mid = mm.code_to_id(MEETING_CODE)
    mid_cache["code"] = MEETING_CODE
    mid_cache["mid"] = mid
    return mid


def quota_info():
    """返回当前额度 / 自动模式状态，供前端决定是否继续自动刷新。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if guard["date"] != today:
        guard["date"], guard["count"], guard["exhausted"] = today, 0, False
    used = guard["count"]
    locked = guard["exhausted"] or (used >= DAILY_SAFE_LIMIT)
    auto_disabled = used >= AUTO_DISABLE_THRESHOLD
    if not AUTO_REFRESH:
        status = "off_config"
    elif locked:
        status = "locked"
    elif auto_disabled:
        status = "auto_disabled_quota"
    else:
        status = "auto"
    return {
        "quota": {
            "used": used,
            "limit": DAILY_SAFE_LIMIT,
            "remaining": max(0, DAILY_SAFE_LIMIT - used),
            "auto_disabled": auto_disabled,
            "locked": locked,
            "auto_enabled": status == "auto",
            "auto_status": status,
        },
        "auto_interval": AUTO_INTERVAL,
    }


def attach_quota(snap):
    """给任意 snapshot 补充实时配额信息（每次请求都重算，缓存命中也不影响额度显示）。"""
    snap.update(quota_info())
    return snap


def friendly_error(msg):
    """把接口原始报错转成人话。"""
    if "500246" in msg or "次数已达上限" in msg:
        return "今日接口调用额度已用完（腾讯会议免费额度按天计算，次日 0 点自动恢复）。可先关掉本页面休息，明天再开。"
    if "无法从会议号解析出" in msg:
        return "暂时解析不到该会议（可能是今日额度已用完，或会议尚未开始）。稍后会自动重试。"
    return msg


def do_poll(src="manual"):
    """真正拉取一次腾讯会议数据（仅在节流周期外被调用）。src='auto' 由前端自动刷新触发。"""
    if DEMO:
        # 演示优先用真实花名册，让界面显示用户自己的名字
        base = mm.load_roster(ROSTER) if ROSTER and os.path.exists(ROSTER) else DEMO_ROSTER
        if base:
            # 模拟：第1人在线，其余缺席；再塞一个无法匹配的昵称演示"待核对"
            online = [base[0]["name"]]
            if len(base) > 1:
                online.append("Guest-iPhone")
        else:
            online = []
        present, absent, unmatched = mm.compare(online, base)
        # 演示：给在线者挂一个过去的入会时间，便于展示在线时长
        now = datetime.now()
        for i, r in enumerate(present):
            mins = (i + 1) * 7
            present[i] = {**r, "join_time": (now - __import__("datetime").timedelta(minutes=mins)).isoformat()}
        snap = make_snapshot(present, absent, unmatched, "DEMO", True,
                             meeting_start=(now - __import__("datetime").timedelta(minutes=30)).isoformat())
        snap["fetched_at"] = time.time()
        return snap

    if not ROSTER or not (MEETING_CODE or MEETING_ID):
        snap = {"error": "未配置会议号(MEETING_CODE/MEETING_ID)或花名册(ROSTER)",
                "updated": datetime.now().strftime("%H:%M:%S")}
        snap["fetched_at"] = time.time()
        return snap

    # —— 防耗尽闸门：每日上限 + 额度耗尽后自动锁定 ——
    today = datetime.now().strftime("%Y-%m-%d")
    if guard["date"] != today:
        guard["date"], guard["count"], guard["exhausted"] = today, 0, False
    if guard["exhausted"]:
        snap = {"error": "今日接口额度已耗尽，已自动锁定不再调用（避免进一步浪费）。请改用「离线导出版」：会议中导出参会名单 xlsx 发我即可出考勤。明日 0 点自动恢复。",
                "updated": datetime.now().strftime("%H:%M:%S")}
        snap["fetched_at"] = time.time()
        return attach_quota(snap)
    if guard["count"] >= DAILY_SAFE_LIMIT:
        snap = {"error": "今日调用已达安全上限 %d 次，已主动停止以保留额度。如需继续，请用「离线导出版」（会议导出 xlsx 发我）或明日再用。" % DAILY_SAFE_LIMIT,
                "updated": datetime.now().strftime("%H:%M:%S")}
        snap["fetched_at"] = time.time()
        return attach_quota(snap)
    # 自动刷新触发的请求：若额度已偏低（用到 90%），自动退场改手动，且不消耗本次次数
    if src == "auto" and guard["count"] >= AUTO_DISABLE_THRESHOLD:
        snap = {"notice": "今日接口额度偏低（已用 %d/%d），自动刷新已暂停以保护额度；请点「🔄 刷新数据」手动刷新，或改用离线导出版。明日 0 点自动恢复。" % (guard["count"], DAILY_SAFE_LIMIT),
                "updated": datetime.now().strftime("%H:%M:%S")}
        snap["fetched_at"] = time.time()
        return attach_quota(snap)

    try:
        roster = mm.load_roster(ROSTER)
        before_calls = mm.API_CALLS
        mid = resolve_mid()
        online, raw = mm.get_participants(mid)
        # 按实际 API 调用次数计（支持翻页，单次查询可能多次调用），至少计 1 次
        guard["count"] += max(1, mm.API_CALLS - before_calls)
        present, absent, unmatched = mm.compare(online, roster)
        # 构建 在线昵称 -> 入会时间 映射，并取会议最早起始时间
        join_map, start_times = {}, []
        for p in raw:
            jt = p.get("join_time")
            dt = parse_iso(jt)
            if dt:
                start_times.append(dt)
                if p.get("left_time") in (None, "", 0, "0"):
                    nm = mm.nick_of(p)
                    if nm:
                        join_map[nm] = jt
        present = [enrich_present(r, online, join_map) for r in present]
        meeting_start = min(start_times).isoformat() if start_times else None
        snap = make_snapshot(present, absent, unmatched, MEETING_CODE or mid, meeting_start=meeting_start)
    except Exception as e:
        msg = str(e)
        # 额度耗尽：立即锁定，之后不再重试，避免雪崩式浪费
        if "500246" in msg or "次数已达上限" in msg or "已达上限" in msg:
            guard["exhausted"] = True
            snap = {"error": "今日接口额度已耗尽（错误码 500246）。已自动锁定不再调用。请改用「离线导出版」：会议中导出参会名单 xlsx 发我即可出考勤。明日 0 点自动恢复。",
                    "updated": datetime.now().strftime("%H:%M:%S")}
            snap["fetched_at"] = time.time()
            return attach_quota(snap)
        # 会议已结束/会议号有变时，清掉解析缓存，下轮重新解析
        if "无法从会议号解析出" in msg or "会议不存在" in msg:
            mid_cache["mid"] = None
        snap = {"error": friendly_error(msg), "updated": datetime.now().strftime("%H:%M:%S")}
    snap["fetched_at"] = time.time()
    return attach_quota(snap)


def maybe_poll(src="manual"):
    """请求驱动 + 节流（手动/首屏/自动各自独立计时）：
       - 手动刷新(🔄按钮)：MANUAL_INTERVAL(30s) 内复用缓存，防连点重复消耗；
       - 首屏点开(open)：OPEN_INTERVAL(180s) 内复用缓存，3分钟内重复打开不重复拉；
       - 自动刷新：AUTO_INTERVAL 独立计时（当前已关闭）。
       配额信息每次实时附加。"""
    now = time.time()
    if src == "manual":
        thr = MANUAL_INTERVAL
    elif src == "open":
        thr = OPEN_INTERVAL
    else:  # auto
        # 自动：按 AUTO_INTERVAL 独立节流，手动刷新不会抑制自动刷新
        if cache["data"] is not None and (now - auto_cache["ts"]) < AUTO_INTERVAL:
            d = dict(cache["data"]); d["_cached"] = True; return attach_quota(d)
        snap = do_poll(src)
        cache["data"] = snap
        cache["ts"] = now
        auto_cache["ts"] = now
        d = dict(snap); d["_cached"] = False
        return attach_quota(d)
    # 手动 / 首屏：按各自阈值节流
    if cache["data"] is not None and (now - cache["ts"]) < thr:
        d = dict(cache["data"]); d["_cached"] = True; return attach_quota(d)
    snap = do_poll(src)
    cache["data"] = snap
    cache["ts"] = now
    d = dict(snap); d["_cached"] = False
    return attach_quota(d)


@app.get("/api/snapshot")
def api_snapshot(src: str = "manual"):
    snap = maybe_poll(src)
    # 禁止任何中间层/浏览器缓存该接口，保证每次刷新都真正打到后端、额度计数实时更新
    return JSONResponse(
        snap,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/")
def index():
    import os
    html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    with open(html, "r", encoding="utf-8") as f:
        content = f.read()
    # 禁止浏览器/中间层缓存页面，保证每次更新后立即生效
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


if __name__ == "__main__":
    import uvicorn, socket, os

    def lan_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    ip = lan_ip()
    print(f">>> 仪表盘启动 | DEMO={DEMO} | 节流间隔={INTERVAL}s | 自动刷新={'开' if AUTO_REFRESH else '关'}({AUTO_INTERVAL}s)")
    print(f">>> 本机访问:   http://localhost:8080")
    print(f">>> 手机同WiFi: http://{ip}:8080   <-- 用这个")
    print(f">>> 自动刷新: 每{AUTO_INTERVAL}s" + ("（默认对齐整点/半点）" if AUTO_INTERVAL >= 1800 else "") + " | 每日安全上限 %d 次，用到 %d%% 自动转手动" % (DAILY_SAFE_LIMIT, int(AUTO_DISABLE_RATIO * 100)))
    print(">>> 请求驱动模式：无页面请求时不调用腾讯会议 API")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
