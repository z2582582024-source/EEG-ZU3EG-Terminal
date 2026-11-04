# 技能包：大模型协作中提炼出的可复用经验

本技能包把项目开发中**出过的错、做对的方法、事故的根因**提炼成四类可直接使用的材料，每一项都写明：
**适用场景、用法、已验证效果、失效条件，以及来自哪次失败或观察。**

| 类别 | 目录 | 内容 |
| --- | --- | --- |
| 提示词工作流 | [`prompt_workflows/`](prompt_workflows/) | 9 个工作流：会话开场、证据优先、声明配证据、数字对账、硬件访问门控、先做廉价阶段 0、**一次性事件与最小必要门禁、结论分级词、执行 / 审核两类对话** |
| 案例模板 | [`case_templates/`](case_templates/) | 3 个模板：事故报告、回归 / 更正报告、对协议敏感的指标结论 |
| 校验脚本 | [`check_scripts/`](check_scripts/) | 5 个本目录脚本（命名、文本格式、资源历史提取、归档对账、删除前核对）+ 包内原处的 7 个脚本（硬件对黄金、位流资格、后综合门等） |
| 踩坑清单 | [`pitfalls/pitfall_list.md`](pitfalls/pitfall_list.md) | 42 条，分硬件访问、仿真构建、指标表述、证据与日志、工具链五组 |

案例来源与完整的提示词 / 回答 / 纠错轨迹：[`../report/ai_collaboration/`](../report/ai_collaboration/)（Claude Code 阶段）与 [`../report/ai_collaboration/codex_era/`](../report/ai_collaboration/codex_era/)（Codex 阶段）。

## 适用范围

* **通用部分**（与具体板卡无关）：提示词工作流 1–4 和 6、三个案例模板、`check_names.py`、`check_doc_format.py`、`verify_tar.py`、`board_delete_verified.py`、
  踩坑清单里 P-01、P-05、P-10–P-14、P-16–P-19、P-21–P-24、P-26–P-28、P-33、P-37–P-40。适合任何“大模型 + 嵌入式 / FPGA 开发”的项目。
* **与 Zynq UltraScale+ / Linux 驱动相关**：硬件访问门控（工作流 5）、一次性事件与最小必要门禁（工作流 7）、P-02、P-03、P-07、P-29、P-32、P-34–P-36、P-41、P-42，以及 `build/`、`sim/` 下的脚本。
  思路适用于“经总线访问可编程逻辑”的任何 SoC，但路径、寄存器和数据包格式需要按新板子改写。
* **与 Vivado 2022.2 相关**：P-04、P-08、P-09、P-20、`summarize_builds.py`。

## 用法（给模型的最短版本）

1. 开新任务前，把 [`pitfalls/pitfall_list.md`](pitfalls/pitfall_list.md) 里对应场景的条目贴进提示词，作为提交前清单。
2. 长任务用 [`prompt_workflows/session_bootstrap.md`](prompt_workflows/session_bootstrap.md) 的骨架写开场提示词。
3. 汇报用 [`prompt_workflows/claim_needs_evidence.md`](prompt_workflows/claim_needs_evidence.md) 的清单自查；数字用 [`number_reconciliation.md`](prompt_workflows/number_reconciliation.md)。
4. 出事故或回归时，用 `case_templates/` 里的模板写报告，并把新坑补进清单。
5. 交付前运行 `check_names.py`、`check_doc_format.py`。

## 提炼过程（失败 → 技能）

```text
失败 / 纠正 / 事故  →  找根因的类别（依据 / 验证对象 / 口径 / 协议 / 访问时机 / 日志来源）
      →  写成一句可判定的检查项  →  能机械检查的写成脚本，需要流程的写成提示词工作流
      →  下一个任务里再用一次  →  在“已验证效果”栏记录有没有避免同类问题
```

例：窗口越界读造成内核 oops（案例 05）→ 检查项“每条访问 PL 的路径先问此刻地址是否有效”（P-02、P-03）→ 驱动里的门控与白名单 + 板上回放程序 → 112,345 次操作 0 错误。
例：BRAM 百分比口径不一致（案例 06）→ “写分母与口径，两版同一报告”（P-05）→ `summarize_builds.py` → 表格与独立重建一致。
例：交付整理时的文本问题（BOM 使一个 Python 文件无法解析、控制台编码造成乱码）→ `check_doc_format.py`，在本包上找出 3 个真实问题并已修复。

## 已验证效果的总体说明

每一条的“已验证效果”只记录在本项目里实际观察到的结果，来源写在条目里；没有验证过的写“未单独验证”。
没有做“有技能包 vs 没有技能包”的对照实验，所以**不声称**技能包提高了整体开发效率；声称的只有：这些检查项各自对应一次真实失败，并且在后续工作中被执行过。

## 关于 PYNQ

作品的开发流程是 Vivado / Vitis / PetaLinux 传统流程，**没有做 PYNQ Overlay，也没有在 PYNQ 平台上验证过本技能包**。
因此不声称这是“通用 PYNQ Skill”。通用部分（见上）与具体板卡无关，换成 PYNQ 板卡后理论上可以直接使用；
这一点没有验证。若要面向 PYNQ，需要补充的内容是：Overlay 加载与比特流 / HWH 配对检查、`pynq.allocate` 缓冲区与缓存一致性、Python 侧的 MMIO 访问门控——这些**不在本包范围内**。

## 失效条件（总览）

* 检查项靠执行：不照清单做就没有效果；能脚本化的已脚本化。
* 针对项目特性的条目（寄存器窗口、复位握手、数据包格式）换了设计就需要改写。
* 工具版本升级后，报告格式相关的脚本需要更新。
