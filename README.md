# 高三16班腾讯会议考勤监控

用腾讯会议 API 自动比对班级花名册，显示在线 / 缺席 / 待核对名单，支持一键复制考勤通报、标记请假（6 小时有效）。内置三级额度保护。

## 两种访问方式

| 方式 | 地址 | 说明 |
|---|---|---|
| **微信内 / 手机** | `https://sunwei-max.github.io/attendance-monitor/` | Pages 静态页，读**同域快照 JSON**（GitHub Actions 定时采集），微信里可直接打开，无需"在浏览器打开" |
| **电脑浏览器（实时）** | 配套的 `*.app.workbuddy.host` 固定链接 | 带 Python 后端，点刷新即真实拉取，数据最实时 |

> 为什么微信里不能直接用后端链接：托管平台对微信内置浏览器 UA 直接返回 403（"请在浏览器打开"拦截页），实测非微信 UA 均 200。故微信侧改为「Actions 定时采集 → 静态 JSON → 同域读取」，规避跨域与 UA 拦截。

## 文件结构

| 文件 | 说明 |
|---|---|
| `index.html` | GitHub Pages 页面（读 `data/latest.json`，同域无跨域） |
| `scripts/snapshot.py` | Actions 快照脚本：拉参会名单 → 比对花名册 → 写 `data/latest.json` |
| `.github/workflows/snapshot.yml` | 定时任务：上课时段每 10 分钟跑一次 |
| `data/latest.json` | 最新快照（由 Action 自动提交） |
| `app/meeting_monitor.py` | 腾讯会议 MCP 封装：解析、分页拉取、在线判定、花名册比对 |
| `app/meeting_dashboard.py` | FastAPI 后端：接口、节流、额度保护、通报格式化 |
| `app/dashboard.html` | 后端版前端单页 |
| `app/start.sh.example` | 配置模板（复制为 `start.sh` 并填入你的 Token） |
| `app/roster.csv` | 示例花名册（真实名单走 GitHub Secret，不入库） |
| `analyze_meeting_export.py` | 离线兜底：读会议导出的 xlsx，零 API 消耗做考勤 |

## 定时快照怎么工作

1. GitHub Actions 在**上课时段（北京时间 8:00–11:50 / 14:00–17:50，周一至周六）每 10 分钟**触发；
2. 从 Secrets 还原真实花名册与 Token → 调腾讯会议接口拉参会名单；
3. 比对后写 `data/latest.json` 并提交；
4. 页面加载即读该 JSON，并**每 60 秒自动重读**，跟上下一次采集。

**Secrets 需要两个**：`WEMEET_TOKEN`（会议 Token）、`ROSTER_B64`（真实花名册 base64）。

额度控制：每次采集约 2 次 API 调用，一天约 48 次采集 ≈ 96 次调用，远低于 150/天的上限。

## 重要说明

- ⚠️ 完整部署步骤、配置项、核心原理、踩坑修复见 [`高三16班腾讯会议考勤监控_复刻指南.md`](高三16班腾讯会议考勤监控_复刻指南.md)。
- `roster.csv` 在仓库中是**脱敏示例**（学生01–60），真实名单只存在 GitHub Secret 与本地。
- 真实 `start.sh`（含 Token）未提交，请用 `start.sh.example` 自行配置。
- Pages 版为**快照**（延迟约 10 分钟）；需要"点开即最新"请用后端链接。
