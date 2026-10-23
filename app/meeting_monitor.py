#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
腾讯会议实时缺席监控器  meeting_monitor.py
=====================================================
依赖：Python 3 标准库 + pandas（读花名册 xlsx/csv 用，沙箱已自带；
      若自行部署缺 pandas：pip install pandas openpyxl）

原理：
  腾讯会议 MCP 工具 get_meeting_participants 可拉取「当前在会」成员明细。
  把【花名册(应到)】与【实时在线名单】做差集，即为「缺席/掉线」的人。
  所谓「实时监控」= 每 N 秒拉一次在线名单并重新比对（接口是查询式，非事件推送）。

用法：
  # 1) 配置 Token（必填，从环境变量读，不写死在脚本里）
  export TENCENT_MEETING_TOKEN='你的token'

  # 2) 实时轮询（每 30 秒刷新一次缺席名单）
  python3 meeting_monitor.py --meeting-code 904854736 --roster 花名册.xlsx --interval 30

  # 3) 只查一次快照（不常驻）
  python3 meeting_monitor.py --meeting-id 123456789 --roster 花名册.csv --once

  # 4) 自检（无真实会议也能验证逻辑是否跑通）
  python3 meeting_monitor.py --demo
=====================================================
"""
import os
import sys
import json
import time
import argparse
import urllib.request
from datetime import datetime

# ---------- 腾讯会议 MCP 连接参数 ----------
BASE_URL = "https://mcp.meeting.tencent.com/mcp/wemeet-open/v1"
SKILL_VERSION = "v1.0.15"
TOKEN = os.environ.get("TENCENT_MEETING_TOKEN", "")

CLIENT_INFO = {"os": "linux", "agent": "workbuddy", "model": "GLM"}

# 累计真实 API 调用次数（供上层准确记录额度消耗；因支持翻页，单次查询可能多次调用）
API_CALLS = 0


def call_tool(name, arguments):
    """调用腾讯会议 MCP 工具，返回原始响应 dict。"""
    global API_CALLS
    if not TOKEN:
        raise RuntimeError("未检测到 TENCENT_MEETING_TOKEN 环境变量，请先 export。")
    arguments = dict(arguments or {})
    arguments["_client_info"] = CLIENT_INFO
    body = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
        "id": 1,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Tencent-Meeting-Token": TOKEN,
        "X-Skill-Version": SKILL_VERSION,
    }
    req = urllib.request.Request(
        BASE_URL, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    API_CALLS += 1
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def unwrap(resp):
    """腾讯会议 MCP 返回是多层包装，需逐层解析出业务 body(dict)。
    真实结构：result.content[0].text 是 JSON 字符串，内含 {status_code, headers, body}，
    其中 body 又是字符串，需二次解析得到真正的业务数据（如 meeting_info_list / participant_list）。
    同时兼容旧假设(result.data.body)与无 status_code 包裹的情况。
    """
    if "error" in resp.get("result", {}):
        raise RuntimeError("MCP 业务错误: " + json.dumps(resp["result"]["error"], ensure_ascii=False))
    text = None
    result = resp.get("result", {})
    content = result.get("content")
    if isinstance(content, list) and content:
        text = content[0].get("text")
    elif isinstance(result.get("data"), dict):
        text = result["data"].get("body")
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = text
    # 网关在参数被自动调整时可能返回 {"_meta":..., "data":{status_code,headers,body}} 包装层
    if isinstance(obj, dict) and "_meta" in obj and isinstance(obj.get("data"), dict):
        obj = obj["data"]
    if not isinstance(obj, dict):
        return {}
    # obj 可能是 {status_code, headers, body} 包装，body 常为字符串
    body = obj.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
    if isinstance(body, dict):
        # 业务层错误（如额度用尽 500246）直接抛出，便于上层给友好提示
        err = body.get("error_info")
        if isinstance(err, dict) and err.get("message"):
            raise RuntimeError("%s（错误码 %s）" % (err["message"], err.get("error_code")))
        return body
    # obj 本身就是业务数据（无 status_code 包裹）
    return obj


def find_key(obj, key):
    """递归在嵌套 dict/list 中找第一个匹配 key 的值。"""
    if isinstance(obj, dict):
        if key in obj and obj[key]:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = find_key(it, key)
            if r:
                return r
    return None


# ---------- 会议号 -> meeting_id ----------
def code_to_id(meeting_code):
    resp = call_tool("get_meeting_by_code", {"meeting_code": str(meeting_code)})
    body = unwrap(resp)
    mid = body.get("meeting_id") or find_key(body, "meeting_id")
    if not mid:
        raise RuntimeError("无法从会议号解析出 meeting_id，请检查会议号是否正确/会议是否存在。")
    return str(mid)


# ---------- 拉取在线参会者（用 end_time 往前翻页） ----------
def get_participants(meeting_id, verbose=False, max_pages=12):
    """返回 (在线昵称列表, 全部参会记录列表)。

    重要：get_meeting_participants 每页最多返回 100 条（page_size 上限=100），
    且实测**不返回 next_page_token / has_more**，但结果是按 join_time 倒序的。
    因此若 total_count > 100，最早的若干条会被截断（这些人往往是一直在线最久的，
    会被误判为缺席）。解决：取本页最早的 join_time 作为下一页的 end_time 往前翻，
    直到覆盖全部记录。记录按 (open_id, join_time) 去重。
    """
    online_nicks = []
    raw_records = []
    seen = set()
    end_time = None
    for page in range(max_pages):
        args = {"meeting_id": meeting_id, "page_size": 100}
        if end_time:
            args["end_time"] = end_time
        resp = call_tool("get_meeting_participants", args)
        body = unwrap(resp)
        plist = body.get("participant_list") or body.get("participants") or []
        total = body.get("total_count")
        if verbose:
            print("[verbose] page=%d args=%s total_count=%s got=%d" % (page + 1, args, total, len(plist)))
        if not plist:
            break
        page_times = []
        for p in plist:
            jt = p.get("join_time")
            if jt:
                page_times.append(jt)
            key = ((p.get("ms_open_id") or p.get("open_id") or ""), jt)
            if key in seen:
                continue
            seen.add(key)
            raw_records.append(p)
            if is_online(p):
                nick = nick_of(p)
                if nick:
                    online_nicks.append(nick)
        if not page_times:
            break
        earliest = min(page_times)
        # 时间无法继续往前推进 → 已到底
        if end_time is not None and earliest >= end_time:
            break
        end_time = earliest
        # 已覆盖全部记录 → 结束
        if total and len(raw_records) >= total:
            break
    return online_nicks, raw_records


def nick_of(p):
    """从参会者记录里取昵称/姓名，兼容多种字段名。"""
    for k in ("user_name", "nick_name", "name", "participant_name", "display_name"):
        v = p.get(k)
        if v:
            return str(v).strip()
    return ""


def is_online(p):
    """判定是否仍在线上：接口真实字段为 left_time，非空即视为已离会。
    兼容 leave_time/leaveTime/leftTime 等历史命名。"""
    for k in ("left_time", "leave_time", "leaveTime", "leftTime"):
        v = p.get(k)
        if v not in (None, "", 0, "0"):
            return False
    return True


# ---------- 花名册加载 ----------
def load_roster(path):
    """读取花名册，返回 [{name, sid}] 列表。支持 csv/xlsx/xls。"""
    try:
        import pandas as pd
    except ImportError:
        raise RuntimeError("读取花名册需要 pandas：pip install pandas openpyxl")
    df = pd.read_excel(path) if path.lower().endswith((".xlsx", ".xls")) else pd.read_csv(path)
    df = df.fillna("")
    # 自动识别姓名列 / 学号列
    cols = list(df.columns)
    name_col = next((c for c in cols if str(c).strip() in ("姓名", "名字", "name", "Name")), None)
    sid_col = next((c for c in cols if str(c).strip() in ("学号", "考号", "sid", "Sid", "ID", "编号")), None)
    if name_col is None:
        # 默认取第一列
        name_col = cols[0]
    roster = []
    for _, row in df.iterrows():
        roster.append({
            "name": str(row[name_col]).strip(),
            "sid": str(row[sid_col]).strip() if sid_col else "",
        })
    roster = [r for r in roster if r["name"]]
    return roster


# ---------- 匹配逻辑 ----------
def matches(nick, r, strict=False):
    n = nick.strip()
    nm = r["name"].strip()
    sid = r["sid"].strip()
    if not n or not nm:
        return False
    if n == nm:
        return True
    if strict:
        return False
    # 宽松：双向包含
    if nm in n or n in nm:
        return True
    # 学号前缀 / 包含
    if sid and (n.startswith(sid) or sid in n):
        return True
    return False


def compare(online_nicks, roster, strict=False):
    """返回 (present, absent, unmatched_online)。"""
    present, absent = [], []
    for r in roster:
        hit = any(matches(nk, r, strict) for nk in online_nicks)
        (present if hit else absent).append(r)
    # 在线但无法对应到花名册的（需人工核对昵称）
    mapped = set()
    for r in present:
        for nk in online_nicks:
            if matches(nk, r, strict):
                mapped.add(nk)
                break
    unmatched = [nk for nk in online_nicks if nk not in mapped]
    return present, absent, unmatched


# ---------- 输出 ----------
def render(round_no, start_total, present, absent, unmatched, meeting_label):
    now = datetime.now().strftime("%H:%M:%S")
    lines = []
    lines.append("=" * 56)
    lines.append(f"[{now}] 轮询第 {round_no} 次 | 会议: {meeting_label}")
    lines.append(f"应到 {start_total} 人 | 在线 {len(present)} 人 | 缺席 {len(absent)} 人")
    if absent:
        lines.append("-" * 56)
        lines.append("【缺席 / 掉线名单】")
        for i, r in enumerate(absent, 1):
            tag = f"({r['sid']})" if r["sid"] else ""
            lines.append(f"  {i:>2}. {r['name']} {tag}")
    if unmatched:
        lines.append("-" * 56)
        lines.append("【在线但昵称无法对应花名册（需人工核对）】")
        for nk in unmatched:
            lines.append(f"  · {nk}")
    if not absent and not unmatched:
        lines.append("✅ 全员在线，无缺席。")
    lines.append("=" * 56)
    return "\n".join(lines)


def write_latest(absent, unmatched, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "absent_latest.csv")
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["状态", "姓名", "学号/编号", "说明"])
        for r in absent:
            w.writerow(["缺席/掉线", r["name"], r["sid"], ""])
        for nk in unmatched:
            w.writerow(["待核对", nk, "", "在线昵称无法匹配花名册"])
    return path


# ---------- 自检模式 ----------
def demo():
    print(">>> 自检模式：用内置模拟数据验证比对逻辑（不调用真实会议）")
    roster = [
        {"name": "张三", "sid": "2027001"},
        {"name": "李四", "sid": "2027002"},
        {"name": "王五", "sid": "2027003"},
        {"name": "赵六", "sid": "2027004"},
    ]
    # 模拟：张三、李四在线；王五掉线（有 leave_time）；赵六没进；另有未知昵称在线
    raw = [
        {"user_name": "张三", "leave_time": ""},
        {"user_name": "李四", "leave_time": ""},
        {"user_name": "王五", "leave_time": "2026-09-28T10:05:00+08:00"},
        {"user_name": "iPhone", "leave_time": ""},
    ]
    online_nicks = [nick_of(p) for p in raw if is_online(p) and nick_of(p)]
    present, absent, unmatched = compare(online_nicks, roster)
    print(render(1, len(roster), present, absent, unmatched, "DEMO"))
    print("\n>>> 自检通过：差集计算、在线判定、昵称匹配均正常。")
    print(">>> 真实使用：export TENCENT_MEETING_TOKEN 后，用 --meeting-code / --roster 运行。")


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(description="腾讯会议实时缺席监控器")
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--meeting-code", help="腾讯会议会议号（9~11位数字）")
    src.add_argument("--meeting-id", help="会议 meeting_id")
    ap.add_argument("--roster", help="花名册路径（csv/xlsx，含姓名/学号列）")
    ap.add_argument("--interval", type=int, default=30, help="轮询间隔秒数（默认30）")
    ap.add_argument("--once", action="store_true", help="只查一次快照后退出")
    ap.add_argument("--strict", action="store_true", help="仅精确匹配昵称=姓名")
    ap.add_argument("--verbose", action="store_true", help="打印原始接口结构")
    ap.add_argument("--demo", action="store_true", help="自检模式")
    ap.add_argument("--out-dir", default="/workspace/meeting_monitor_output", help="结果输出目录")
    args = ap.parse_args()

    if args.demo:
        demo()
        return

    if not (args.meeting_code or args.meeting_id):
        ap.error("请提供 --meeting-code 或 --meeting-id（或加 --demo 自检）")
    if not args.roster:
        ap.error("请提供 --roster 花名册路径")

    roster = load_roster(args.roster)
    if not roster:
        ap.error("花名册为空或无法解析姓名列")
    print(f">>> 已加载花名册：应到 {len(roster)} 人")

    try:
        meeting_id = args.meeting_id or code_to_id(args.meeting_code)
    except Exception as e:
        print("[错误] " + str(e))
        sys.exit(1)
    label = args.meeting_code or meeting_id
    print(f">>> 目标会议 meeting_id={meeting_id}")

    round_no = 0
    try:
        while True:
            round_no += 1
            online_nicks, _ = get_participants(meeting_id, verbose=(args.verbose and round_no == 1))
            present, absent, unmatched = compare(online_nicks, roster, strict=args.strict)
            text = render(round_no, len(roster), present, absent, unmatched, label)
            print(text)
            path = write_latest(absent, unmatched, args.out_dir)
            if args.once:
                print(f">>> 快照已写入：{path}")
                break
            print(f">>> {args.interval} 秒后再次轮询...（Ctrl+C 停止）")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n>>> 已停止监控。")
    except Exception as e:
        print("[错误] " + str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
