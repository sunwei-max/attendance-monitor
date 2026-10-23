#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时快照脚本（GitHub Actions 专用）

作用：调用腾讯会议 MCP 拉一次参会名单 → 与花名册比对 → 写出 data/latest.json
      供 GitHub Pages 静态页面（同域）直接读取，规避微信内置浏览器的跨域/UA 拦截。

环境变量：
  TENCENT_MEETING_TOKEN  腾讯会议 Token（来自 GitHub Secrets）
  MEETING_CODE           会议号，默认 4440247767
  ROSTER_PATH            花名册路径，默认 app/roster.csv
"""
import json
import os
import sys
import pathlib
from datetime import datetime, timezone, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import meeting_monitor as mm  # noqa: E402

TZ = timezone(timedelta(hours=8))
CODE = os.environ.get("MEETING_CODE", "4440247767").strip()
ROSTER = pathlib.Path(os.environ.get("ROSTER_PATH", str(ROOT / "app" / "roster.csv")))
OUT = ROOT / "data" / "latest.json"


def now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def build_snapshot():
    if not os.environ.get("TENCENT_MEETING_TOKEN"):
        return {"error": "服务端未配置 Token（GitHub Secret 缺失）"}

    if not ROSTER.exists():
        return {"error": "服务端未找到花名册（GitHub Secret 缺失）"}

    roster = mm.load_roster(str(ROSTER))
    mid = mm.code_to_id(CODE)
    online, raw = mm.get_participants(mid)
    present, absent, unmatched = mm.compare(online, roster)

    # 入会时间映射（仅取仍在线者）
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

    def enrich(r):
        out = {"name": r["name"], "sid": r.get("sid", ""), "join_time": None}
        for nk, jt in join_map.items():
            if mm.matches(nk, r):
                out["join_time"] = jt
                break
        return out

    present_rows = [enrich(r) for r in present]
    meeting_start = min(start_times).isoformat() if start_times else None

    return {
        "total": len(present_rows) + len(absent),
        "present": present_rows,
        "absent": [{"name": r["name"], "sid": r.get("sid", "")} for r in absent],
        "unmatched": unmatched,
        "label": CODE,
        "demo": False,
        "meeting_start": meeting_start,
        # 在线人数为 0 通常说明当前没在开会（或已散会），前端据此不报"全班缺席"
        "meeting_active": len(present_rows) > 0,
        "updated": datetime.now(TZ).strftime("%H:%M:%S"),
        "generated_at": now_str(),
        "generated_ts": int(datetime.now(TZ).timestamp()),
        "api_calls": mm.API_CALLS,
        "source": "github-actions",
    }


def main():
    try:
        snap = build_snapshot()
    except Exception as e:
        snap = {"error": "拉取失败：%s" % e}

    snap.setdefault("generated_at", now_str())
    snap.setdefault("generated_ts", int(datetime.now(TZ).timestamp()))
    snap["source"] = "github-actions"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # 保留最近若干条历史，便于排查（只留 6 条）
    hist_path = OUT.parent / "history.json"
    hist = []
    if hist_path.exists():
        try:
            hist = json.loads(hist_path.read_text(encoding="utf-8"))[:5]
        except Exception:
            hist = []
    if not snap.get("error"):
        hist.insert(0, {
            "t": snap.get("generated_at"),
            "online": len(snap.get("present", [])),
            "absent": len(snap.get("absent", [])),
        })
    hist_path.write_text(json.dumps(hist[:6], ensure_ascii=False, indent=1), encoding="utf-8")

    OUT.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print("写出 %s" % OUT)
    print(json.dumps({k: snap.get(k) for k in ("total", "error", "meeting_active", "api_calls", "generated_at")},
                     ensure_ascii=False))
    print("在线 %d / 缺席 %d" % (len(snap.get("present", [])), len(snap.get("absent", []))))
    if snap.get("error"):
        print("!! 注意：本次快照为错误状态，已写入（前端会展示该错误）")


if __name__ == "__main__":
    main()
