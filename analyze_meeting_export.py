#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
腾讯会议离线考勤分析（API 额度耗尽时的兜底方案）
=====================================================
把会议里「导出参会名单」得到的 xlsx 与班级花名册做差集，
算出缺席 / 到会。完全离线，不消耗任何 API 额度。

适用场景：
  1) 腾讯会议 API 免费额度用完（错误码 500246）时；
  2) 不想消耗 API 额度、只想偶尔查一次时。

用法：
  python3 analyze_meeting_export.py 会议导出.xlsx
  python3 analyze_meeting_export.py 会议导出.xlsx --roster 花名册.csv

产出两份报告（文件名自动带日期，不覆盖历史）：
  考勤分析_<会议号>_<YYYYMMDD>.txt   完整版（含到会明细）
  考勤通报_<会议号>_<YYYYMMDD>.txt   简洁版（直接转发 / 打印）
"""
import sys, re, os
from datetime import datetime
import pandas as pd

DEFAULT_ROSTER = "/workspace/app/roster.csv"
SHORT_MIN = 5  # 累计时长小于该分钟数视为「短时到会」，列入重点关注


def parse_roster(path):
    ro = pd.read_csv(path, header=None)
    names = [str(x).strip() for x in ro.iloc[:, 0].tolist()]
    return [x for x in names if x and x != "nan" and x != "姓名"]


def parse_attendees(excel_path):
    df = pd.read_excel(excel_path, sheet_name="成员观看概况", header=None)
    hdr = df[df.apply(lambda r: r.astype(str).str.contains("用户昵称").any(), axis=1)].index
    start = (int(hdr[0]) + 1) if len(hdr) else 9
    attendees = []
    for _, r in df.iloc[start:].iterrows():
        nick = str(r[1]).strip()
        if not nick or nick == "nan":
            continue
        attendees.append({
            "nick": nick,
            "first": str(r[6]).strip(),
            "last": str(r[7]).strip(),
            "cnt": str(r[8]).strip(),
            "dur": str(r[9]).strip(),
        })
    return df, attendees


def dur_to_min(s):
    try:
        h, m, sec = [int(x) for x in s.split(":")]
        return h * 60 + m + (1 if sec >= 30 else 0)
    except Exception:
        return 999


def meeting_no(df):
    s = df.to_string()
    m = re.search(r"会议号[:：]\s*(\d+)", s)
    return m.group(1) if m else "unknown"


def main():
    if len(sys.argv) < 2:
        print("用法: python3 analyze_meeting_export.py 会议导出.xlsx [--roster 花名册.csv]")
        sys.exit(1)
    excel_path = sys.argv[1]
    roster_path = DEFAULT_ROSTER
    for i, a in enumerate(sys.argv):
        if a == "--roster" and i + 1 < len(sys.argv):
            roster_path = sys.argv[i + 1]

    if not os.path.exists(excel_path):
        print("找不到 Excel 文件：", excel_path); sys.exit(1)
    if not os.path.exists(roster_path):
        print("找不到花名册：", roster_path); sys.exit(1)

    df, attendees = parse_attendees(excel_path)
    names = parse_roster(roster_path)
    mno = meeting_no(df)
    date_tag = datetime.now().strftime("%Y%m%d")

    present, absent, unmatched = [], [], []
    for n in names:
        hit = [a for a in attendees if n in a["nick"]]
        (present.append((n, hit[0])) if hit else absent.append(n))
    for a in attendees:
        if not any(n in a["nick"] for n in names):
            unmatched.append(a)

    short = [(n, a) for n, a in present if dur_to_min(a["dur"]) < SHORT_MIN]

    # ---- 完整版 ----
    L = []
    L.append("高三16班 出勤分析（会议 %s）" % mno)
    L.append("数据来源：手动导出的「成员观看概况」Excel —— 离线分析，完全不受 API 额度限制")
    L.append("")
    L.append("【统计结果】")
    L.append("  应到（花名册）：%d 人" % len(names))
    L.append("  已匹配到会：%d 人" % len(present))
    L.append("  疑似缺席：%d 人" % len(absent))
    L.append("  非本班/异常昵称（需确认）：%d 人" % len(unmatched))
    L.append("")
    L.append("【缺席名单】")
    for i, n in enumerate(absent, 1):
        L.append("  %d. %s" % (i, n))
    L.append("")
    L.append("【重点关注 · 短时到会（疑似来了就走）】")
    if short:
        for n, a in short:
            L.append("  · %s  首次%s  时长%s  退会%s" % (n, a["first"], a["dur"], a["last"]))
    else:
        L.append("  （无）")
    L.append("")
    L.append("【需确认的非本班/异常昵称】")
    for a in unmatched:
        st = "仍在会" if a["last"] in ("--", "nan", "") else ("退会 " + a["last"])
        L.append("  · %s   首次%s  时长%s  %s" % (a["nick"], a["first"], a["dur"], st))
    L.append("")
    L.append("【已到会明细 %d 人】" % len(present))
    L.append("  #   姓名      首次入会时间          累计时长   状态")
    for i, (n, a) in enumerate(present, 1):
        st = "仍在会" if a["last"] in ("--", "nan", "") else "已退会"
        L.append("  %2d  %-6s  %-20s %-8s %s" % (i, n, a["first"], a["dur"], st))

    full = "考勤分析_%s_%s.txt" % (mno, date_tag)
    with open(full, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    # ---- 简洁通报版 ----
    S = []
    S.append("高三16班 出勤通报")
    S.append("会议：%s（个人会议室）" % mno)
    S.append("应到 %d 人 ｜ 实到 %d 人 ｜ 缺席 %d 人" % (len(names), len(present), len(absent)))
    S.append("")
    S.append("【缺席名单（%d人）】" % len(absent))
    for i, n in enumerate(absent, 1):
        S.append("  %d. %s" % (i, n))
    if short:
        S.append("")
        S.append("【重点关注 · 短时到会】")
        for n, a in short:
            S.append("· %s 仅停留 %s（%s 进、%s 退），需核实" % (n, a["dur"], a["first"][-8:], a["last"][-8:]))
    S.append("")
    S.append("【需老师确认（非本班昵称，不计入缺席判定）】")
    for a in unmatched:
        tag = "仍在会" if a["last"] in ("--", "nan", "") else ("退会 " + a["last"])
        S.append("· %s（%s）" % (a["nick"], tag))
    S.append("")
    S.append("说明：本报告依据手动导出的「成员观看概况」离线生成，与腾讯会议 API 额度无关。")

    brief = "考勤通报_%s_%s.txt" % (mno, date_tag)
    with open(brief, "w", encoding="utf-8") as f:
        f.write("\n".join(S))

    print("\n".join(S))
    print("\n已生成：\n  %s\n  %s" % (full, brief))


if __name__ == "__main__":
    main()
