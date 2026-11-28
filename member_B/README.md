# 角色 B · John Deere Owner Trust 2024-B 汇总池模型

**当前状态：Day 1–3 已完成并通过；报告 §4–§8 初稿已出**（2026-11-28）。
报告见 [`report/john_deere_2024b_report.md`](../report/john_deere_2024b_report.md)。
**G1–G6 共 751 项校验全 pass**：历史回放把 29 个月的 Note Value 复现到平均 **0.246%** 误差，
瀑布把 145 个档月的本金/利息/期末余额复现到分（证书残差最大 **0.0127 美元**），
实绩 WAL/IRR 已算出。工作计划见 [`plans/plan_b_workplan.md`](../plans/plan_b_workplan.md)，
待确认问题见 [`plans/open_questions_b.md`](../plans/open_questions_b.md)。

## 运行

```powershell
$py = "C:\Users\wcs\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

& $py member_B\run_all.py --stage all      # data + spike + feedback + day2 + day3，全部门槛
& $py member_B\run_all.py --stage data     # 只做数据层
& $py member_B\run_all.py --stage spike    # 校准 + 回放 + G1–G3
& $py member_B\run_all.py --stage day2     # 瀑布 + WAL/IRR + G4/G5
& $py member_B\run_all.py --stage day3     # 情景 + 网格 + 图 + G6
```

退出码非零代表有门槛失败，**此时不要使用 outputs/**。

## 环境（本机实测，无法联网）

| 项 | 值 |
|---|---|
| 解释器 | `C:\Users\wcs\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`（Python 3.12.14） |
| 可用 | numpy 2.3.5、pandas 3.0.1、XlsxWriter 3.2.9、openpyxl 3.1.5、python-docx 1.2.0、python-pptx 1.0.2、Pillow 12.3.0 |
| **不可用** | `matplotlib`、`scipy`、`pypdf`、`pytest`；PyPI 不可达，无法安装 |

- `python` 不在 PATH 上（Windows 会转到 Microsoft Store 别名），必须用上表的绝对路径。
- 图表走 XlsxWriter 原生 Excel 图表 + 纯标准库 SVG，**不**引入 matplotlib。
- `member_A/extract_prospectus.py` 依赖 pypdf，在本机无法运行；替代方案见下。

## 已完成的工具与模块

| 文件 | 作用 |
|---|---|
| `tools/pdf_text.py` | 标准库 `re`+`zlib` 离线提取/检索招股书文本 |
| `src/data_cleaning.py` | 读 A 的 13 张表 → 规范化月度面板；列字典映射；结构性质检查 |
| `src/pool_model.py` | 收益率求解、无条件摊销路径、8.85% 现值曲线、状态递推、历史回放、实绩统计 |
| `src/waterfall.py` | 10 顺位瀑布、分档本金/利息、短缺结转、储备、证书残余 |
| `src/metrics.py` | WAL（ACT/365）、IRR（三套价格口径）、清偿月份 |
| `src/stress_test.py` | 情景引擎、二维网格、法定到期检查 |
| `src/charts.py` | 纯标准库 SVG：折线、柱状、热力图 |
| `src/validate.py` | G1–G6 门槛，输出 A 的表结构 |
| `tools/` + `reference/` | 招股书文本工具；角色 A 于 2026-11-26 提供的 5 个附件（含 SHA-256） |
| `run_all.py` | 一条命令重建全部结果表 |

```powershell
# 招股书全文导出（缓存到 member_B/raw/）
& $py member_B\tools\pdf_text.py dump
# 关键字检索（不区分大小写、去空白匹配）
& $py member_B\tools\pdf_text.py find "note value" "8.85" "clean-up call"
```

已验证：能完整还原印刷页 59 的 "Original Scheduled Payments" 表（73 期），
以及 Note Value / 违约应收款估值 / 本金顺序的原文定义。

## 结果索引（`outputs/tables/`）

| 文件 | 内容 |
|---|---|
| `panel_pool.csv` | 29×50 清洁池表面板（含 `t`、`collection_index`、期间消耗的计划月数） |
| `panel_note_cashflows.csv` | 145 档月，带 `is_live` 标记，剔除了已清偿档的零行干扰 |
| `quality_B.csv` | 6 项结构性质检查（1 项 `review`：负提前还款待 A 定义） |
| `calibration.csv` | 池收益率两种口径、8.85% 折现、初始现值、回放误差汇总 |
| `replay_note_value.csv` | 29 期回放全字段（PV、ρ、存活比、预测/实际、移除额三种口径） |
| `replay_focus.csv` | 回放的精简视图 |
| `history_facts.csv` | 实绩基准（余额、损失、提前还款、速度、逾期） |
| `Validation_B.csv` | 154 条门槛明细（A 的列结构 + `gate`/`left`/`right`/`note`） |
| `gate_summary.csv` | 按门槛汇总 |
| `waterfall_tranches.csv` | 145 档月的模型瀑布（本金/利息/期末余额/利息短缺） |
| `waterfall_periods.csv` | 29 期完整顺位栈（费用、储备、证书、现金摊完校验） |
| `returns_historical_issue.csv` / `_par.csv` | 实绩 WAL/IRR，分别按发行价与面值 |
| `returns_reference_disclosed.csv` | 同一指标但从披露现金流独立计算（G5 的对照） |
| `Validation_day2.csv` | G4/G5 明细 |
| `scenario_results.csv` | 7 个情景 × A-3/A-4 的 WAL、IRR、清偿月、短缺 |
| `loss_absorption.csv` | 损失吸收阶梯：增量损失由证书 / 储备 / 票据分别承担多少 |
| `grid_surface.csv` | 42 格二维网格（损失 × CPR） |
| `../charts/*.svg` | 6 张图：Note Value 路径、三情景余额、A-3/A-4 WAL 热力图、证书现金热力图、WAL 柱状图 |
| `feedback_split_comparison.csv` | 季节性与平滑两套拆分的并列对比（A 的 B3 要求） |
| `feedback_alignment_sensitivity.csv` | 期间对齐前后移一期的敏感性（A 的 A4 要求） |
| `feedback_removal_reconciliation.csv` | 移除额对账：回购现金 32.05M vs 本金 20.34M（A 的 A3） |

## 核心结论（详见计划书 §0、§2.3、§4.2、§4.3、§6.2）

1. 初始 Note Value = 8.85% 月复利下 73 期计划回款的现值，复算差 **0.0029 美元**。
2. 全模型只需一个校准参数（池摊销费率）：平滑口径 5.5238%（含应计 4.8240%，与披露 APR 差 8bp）；
   **季节性口径 5.7541%（含应计 5.0236%，差 28bp）—— 采用季节性**，因为它让 Note Value 误差减半。
   费率是摊销因子而非合同 APR，两个口径的数字都保留在 `calibration.csv`。
3. 历史回放（季节性）：平均绝对误差 **0.246%**、最大 **0.659%**、期末 **−0.024%**。
4. 瀑布逐档到分复现：551 项全 pass，证书残差最大 **0.0127 美元**，本金/利息短缺恒为 0。
5. 实绩（按发行价，ACT/365）：A-1 WAL 0.3431 年 / IRR 5.732%；A-2A 1.1849 / 5.548%；A-2B 1.1849 / 5.271%。
   A-3、A-4 未到期，IRR 不可算（表中留 NaN，不用 0 冒充）。
6. 现金与池余额**无法**逐月闭合（反解年化利率 −2.7%~+6.8% 摆动）；模型只保证结构恒等式。

## 约定

- 输入只读自 `member_A/data/`，不改动 `member_A/` 下任何文件。
- 所有产出写入 `member_B/outputs/`，编码 `utf-8-sig`（与 A 的 CSV 一致）。
- 代码中每个假设参数都要在 `docs/assumptions.md` 登记（参数名 / 取值 / 来源 / 依据 / 敏感性区间 / 代码位置）。
