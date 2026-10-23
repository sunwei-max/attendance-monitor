# 高三16班腾讯会议考勤监控

用腾讯会议 API 自动比对班级花名册，实时显示在线 / 缺席 / 待核对名单，支持一键复制考勤通报、标记请假。纯手动刷新，内置三级额度保护。

## 快速开始

**完整部署步骤、配置项、核心原理、踩坑修复都在 [`复刻指南.md`](复刻指南.md)**，照着做即可从零复刻。

## 文件结构

| 文件 | 说明 |
|---|---|
| `app/meeting_monitor.py` | 腾讯会议 MCP 封装：解析、分页拉取、在线判定、花名册比对 |
| `app/meeting_dashboard.py` | FastAPI 后端：接口、节流、额度保护、通报格式化 |
| `app/dashboard.html` | 前端单页：名单 / 图表 / 刷新 / 复制 / 请假标记 |
| `app/start.sh.example` | 配置模板（复制为 `start.sh` 并填入你的 Token） |
| `app/roster.csv` | 示例花名册（一列「姓名」，请替换为你的班级） |
| `analyze_meeting_export.py` | 离线兜底：读会议导出的 xlsx，零 API 消耗做考勤 |

## 重要说明

- ⚠️ 本项目需自建运行环境（FastAPI + 腾讯会议 Token），**不适用于纯静态 GitHub Pages 托管**；若要一个"打开就能用"的网址，请用配套的固定分享链接。
- `roster.csv` 含真实学生姓名，仓库默认**私有**；如需公开分享，请先脱敏或确认合规。
- 真实 `start.sh`（含 Token）未提交，请用 `start.sh.example` 自行配置。
