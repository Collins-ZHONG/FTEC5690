# Verizon 设备应收款 ABS 项目计划

## 1. 项目定位

### 建议题目

**设备分期应收款的提前结清、换机和注销如何影响 Verizon Master Trust 各档票据的现金流稳定性？**

本项目必须先锁定一个具体系列，不要只写“Verizon ABS”。当前浏览到的公开发行文件为 Verizon 2020-A 相关的 424B5 页面；正式报告中应以该系列的最终招股书、投资者报告和 SEC 申报为准。

## 2. 研究特色

Verizon 设备应收款的重点不是车辆回收，而是应收款生命周期：

- 客户按月支付设备分期款；
- 设备升级或换机可能导致提前结束；
- 客户注销、违约或回购会改变应收款池；
- 超额利差、储备账户、次级和发行人支持机制吸收损失。

这个切口比泛做“汽车 ABS 信用风险”更具体，也有助于降低撞题风险。

## 3. 数据来源与数据表

先确认具体系列是否有完整的：

- 424B5 或最终招股书；
- master trust 交易协议；
- 月度或季度投资者报告；
- SEC 10-D、8-K 或其他定期申报；
- 评级机构 presale 或 surveillance report。

### ReceivablePerformance

| period | beginning_balance | collections | charge_offs | prepayments | repurchases | ending_balance |
|---|---:|---:|---:|---:|---:|---:|
| month | USD | USD | USD | USD | USD | USD |

### TrancheData

| tranche | balance | coupon | priority | enhancement | legal_final_maturity |
|---|---:|---:|---|---:|---|

### SupportMechanics

| mechanism | trigger | cash_flow_effect | investor_protection |
|---|---|---|---|
| Repurchase | 不合格或特定回购条件 | 移除资产并补入现金 | 降低池内损失 |
| Excess spread | 回收额超过费用和利息 | 吸收净损失 | 优先保护高级档 |
| Reserve account | 账户有可用余额 | 补足现金短缺 | 延后本金损失 |
| Subordination | 次级档余额仍可吸收损失 | 次级档先承担损失 | 保护高级档 |

## 4. 建模范围

### 第一层：应收款余额

```text
Ending balance
= Beginning balance
+ New receivables
- Scheduled collections
- Prepayments
- Charge-offs
- Repurchases
```

如果没有完整的新发放数据，可以固定池规模，重点模拟提前结清、注销、损失和回购对存量应收款的影响。

### 第二层：票据现金流

每月依次：

1. 计算设备分期款回收；
2. 扣除服务费和其他优先费用；
3. 扣除净损失；
4. 计提各档票据利息；
5. 按 waterfall 分配本金；
6. 计算各档 WAL、IRR、余额和损失。

### 第三层：发行人支持

设置可切换的模型变量：

```python
repurchase_support = True
excess_spread = True
reserve_account = True
```

至少比较“正常支持”“支持受限”和“无额外支持”三种情况。

## 5. 压力测试

| 情景 | 提前还款/提前终止 | 净损失 | 回购支持 | 研究意义 |
|---|---:|---:|---|---|
| Base | 历史水平 | 历史水平 | 正常 | 复制交易表现 |
| Slow-pay | 降低 30% | 不变 | 正常 | 观察回收延后 |
| High churn | 提高 50% | 不变 | 正常 | 模拟换机和提前结束 |
| Stress default | 不变 | 提高 2 倍 | 受限或关闭 | 检验信用增强是否足够 |

关键输出：

- 优先档是否按期付息；
- 优先档 WAL 变化；
- 次级档损失；
- 超额利差消耗速度；
- 回购和发行人支持对损失吸收的贡献。

## 6. FinTech 应用

可讨论但应区分“已用数据验证”和“未来可行方案”：

- 用账户级数据预测提前换机和注销；
- 用机器学习更新客户提前结清概率；
- 用实时数据更新 ABS 现金流预测；
- 用异常检测识别重复回购或不合格应收款；
- 用智能合约记录应收款转让和回购。

## 7. 代码结构

```text
verizon_data_cleaning.py
receivable_termination.py
verizon_waterfall.py
support_scenarios.py
charts.py
```

建议先完成汇总数据版本，再决定是否扩展到更细的数据。如果具体系列没有逐笔数据，不影响项目完成，但应把模型定位为“汇总应收款生命周期模型”。

## 8. 报告结构

1. 具体系列和交易目的；
2. master trust、发行人、服务商和受托人；
3. 设备付款计划和应收款生命周期；
4. 数据质量和披露限制；
5. 回购、超额利差、储备账户和次级安排；
6. 现金流 waterfall；
7. 换机、提前结清、注销和损失压力测试；
8. 投资者评估；
9. 模型限制、治理和可复现性；
10. FinTech 改进方案。

## 9. 交付节奏

### 第 1 天：锁定交易

- 确认具体系列名称、CIK、文件编号和最终招股书；
- 下载或整理至少 6 个月的投资者报告；
- 建立字段字典并确认是否有逐笔数据。

### 第 2 天：建数据和余额模型

- 清理回收、损失、提前结清和回购数据；
- 完成应收款余额变化模型；
- 记录所有缺失值和替代假设。

### 第 3 天：建 waterfall 和压力测试

- 加入票据利息、本金和信用增强；
- 完成四组压力情景；
- 生成 WAL、IRR、损失和余额图表。

### 第 4 天：完成报告和复核

- 核对合同条款与代码；
- 写清数据限制和模型治理；
- 检查数据、代码、报告可以从空目录重新运行。

## 10. 主要风险与控制

| 风险 | 控制方法 |
|---|---|
| 具体系列文件不完整 | 第一天设定停止条件；若缺少关键条款，改做汇总现金流版本 |
| Master trust 结构复杂 | 只建与研究问题直接相关的账户和 waterfall |
| 回购条款理解错误 | 以最终招股书和交易协议原文为准，单独列条款映射表 |
| 逐笔数据不可得 | 不强行做贷款级预测，改用账户生命周期汇总模型 |
| 新发放数据缺失 | 固定池规模，重点研究提前终止和损失情景 |

## 11. 预期成果

- 一个具体 Verizon 系列的可复现数据集；
- 应收款生命周期和现金流 waterfall 模型；
- 换机、提前结清、注销及损失压力测试；
- 对 Verizon 支持机制和投资者风险的量化评估。

## 12. 二人并行分工

| 成员 | 主线任务 | 可并行产出 |
|---|---|---|
| 成员 A：交易与数据 | 锁定具体系列；收集 424B5、交易协议、投资者报告和 10-D；整理回收、提前终止、损失和回购数据 | 文件清单、数据字典、ReceivablePerformance 和 SupportMechanics |
| 成员 B：结构与模型 | 阅读 waterfall 和发行人支持条款；编写余额模型、票据现金流和支持机制开关；运行四组压力测试 | 条款映射、可运行模型、情景结果和图表 |

### 合并节点

成员 A 先确认具体系列和字段可得性，成员 B 同时搭建不依赖最终数据的模型框架。拿到条款后，成员 A 核对字段与报告口径，成员 B 接入数据；最后共同复核回购、超额利差和储备账户的处理。

## 12. 参考文件

- 当前核对的 Verizon 424B5 页面: https://www.sec.gov/Archives/edgar/data/1737286/000092963820000087/verizon424b5.htm
- Verizon 投资者证券化入口: https://www.verizon.com/about/investors/asset-backed-securitization
