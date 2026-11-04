# 工作流 8：结论分级词（claim classification）

**适用场景**：让 AI 代理汇报“通过 / 完成 / 验证”时，防止把弱证据写成强结论；评审材料里的状态表。

## 用法

先约定一组固定状态词，要求代理**只能**用这些词给结论定级，并写出每个词的适用范围：

| 状态词 | 含义 | 不能推出的 |
| --- | --- | --- |
| `SOFTWARE_SIMULATION_ONLY` | 只有 CPU / Python / 模拟数据结果 | 不是 RTL、不是硬件 |
| `ASSERTION_SIMULATION_PASS / BOUNDED_PASS` | 指定夹具、参数、场景内的断言仿真通过 | 不是形式证明，不覆盖未列场景 |
| `NOT_PASSED（证据未闭环）` | 证据有缺口，**不等于**明确硬件失败 | 不得写成“硬件坏了” |
| `NOT_SIGNED_OFF` | 外部 I/O 时序、完整系统时序等尚未签核 | 内部时序通过不能替代 |
| `NOT_INTEGRATED` | 路径只在有限范围证明，未并入完整产品 | 有限路径通过 ≠ 产品完成 |
| `LIMITED_REAL_DMA_INTERNAL_TEST_PATH_PASS` | 真实硬件、有限范围（内部测试信号、有限帧数）通过 | 不是连续采集、外部输入或人体采集 |
| `BOARD_VERIFIED` | 板上实测通过 | 只在所述条件下成立 |
| `NOT_EXERCISED` | 设计存在但没有跑过 | 不得写成通过 |
| `UNKNOWN` | 没有直接证据 | 客户端退出 / 日志缺失都不是证据 |

汇报里每个结论都要写：状态词 + 适用范围 + 证据位置；`UNKNOWN` 与 `NOT_EXERCISED` 单独成栏，不许省略。

## 已验证效果

Codex 阶段的开发文档统一使用这些词（例如 4C 长期保持 `NOT_PASSED`，ID 读通后只写“两片各三次 0x3E 通过，完整 4C 仍未通过”；事件 08 写作 `LIMITED_REAL_DMA_INTERNAL_TEST_PATH_PASS`）。
本包的 `report/metrics.md` 第 7 节“尚未测量的项目”与 `report/design_report.md` 第 9 节的状态表沿用了同样的做法。

## 失效条件

状态词本身要有人维护；词表膨胀后没人记得含义。建议词表不超过十来个，并在报告里附上这张表。

## 来源

案例 C-01、C-03；状态索引 §2–§3。
