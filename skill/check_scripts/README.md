# check_scripts：可执行的校验脚本

每个脚本说明：适用场景 / 用法 / 已验证效果 / 失效条件 / 来源。标“包内原处”的脚本不重复拷贝，路径如下。

## 本目录的脚本

| 脚本 | 适用场景 | 用法 | 已验证效果 | 失效条件 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `check_names.py` | 交付树的命名规范：全部英文小写 / 数字 / `_` / `-` / `.`（`README.md`、`CMakeLists.txt`、`Makefile`、`__init__.py`、`__main__.py` 例外），并查大小写冲突 | `python check_names.py <根目录>`；退出码 1 = 有违规 | 本包 3,202 个名字 0 违规；对故意构造的含中文目录与大写名的样例报告违规并返回 1 | 只看名字，不看内容 | 选题指南 3.3.5 的目录命名要求 |
| `check_doc_format.py` | 文本文件的 UTF-8 / BOM / NUL / U+FFFD / 换行 | `python check_doc_format.py <根目录> [--strict-lf]`；退出码 1 = 有问题 | 在本包上找出 3 个真实问题（2 个 BOM，其中一个使 `target_table.py` 无法解析；1 个乱码日志），修复后 0 问题；对故意构造的 BOM / CRLF 样例返回 1 | 只检查指定扩展名；CRLF 默认只计数 | 案例 08.2（换行）与本轮交付整理 |
| `summarize_builds.py` | 从多次 Vivado 构建目录的利用率与时序报告提取资源 / 时序历史表 | `python summarize_builds.py <构建根目录> out.tsv out.md`（构建名列表在脚本顶部） | 6 次构建的表与原始报告一致，且与从源码重建的结果对账一致 | 报告格式变了会报错退出（不返回默认值） | 案例 06 |
| `verify_tar.py` | 备份归档与预期 SHA 清单对账（硬链接成员按其目标内容计，符号链接要存成链接） | `python -I verify_tar.py <归档.tar> <预期.sha> <links.txt>` | 用于板端数据备份归档与预期清单的对账 | 预期清单本身错误 | “删除规程”：先拷到主机、逐文件校验 |
| `board_delete_verified.py` | 板上批量删除前后的核对：每个文件必须存在且 SHA 与预期一致，目录里不能有额外文件 / 符号链接，任何不一致都在删除前中止整批 | `python board_delete_verified.py <根> <预期.sha> <目录清单> [--apply]`（先不带 `--apply` 试跑） | 设计为任何不一致都在删除前中止整批（本包没有单独演示） | 目录清单与预期不匹配 | 案例 08.4 |

## 包内原处的脚本（直接使用）

| 脚本 | 适用场景 | 用法 | 已验证效果 |
| --- | --- | --- | --- |
| `sim/golden/compare_hw_to_golden.py` | 把板上抓取的 EDP3 数据包与黄金模型逐字比对 | `python sim/golden/compare_hw_to_golden.py <抓取.bin> [<抓取.bin> …]`（板上抓取的 EDP3 数据包） | 153 个包 654,687 字 0 不一致（`board/measured/pair359/compare_hw_to_golden.txt`） |
| `sim/golden/compare_tap_capture.py` | 把 SSVEP 抽头抓到的帧与黄金抽取器逐帧比对，并扫描起点偏移 | 同上 | 对齐起点 800/800 相同，偏移起点只有部分相同，说明判据有效 |
| `board/tools/ssvep_ops_replay.c` | 把仿真用的操作程序 `ops.hex` 在真实 PL 上回放 | `ssvep_ops_replay ops.hex [/dev/eeg-dsp] [--stats]` | 112,345 次操作 0 错误 |
| `build/qualify/qualify_ter5_bit.py` | 位流资格检查：布线检查点审计 + Bootgen 转换 + 字序逐字验证 | 见 `build/README.md` | 用于 TER4 / TER5 位流 |
| `build/pair/ter5_pair_update.py` | 位流与模块成对更新，先备份、原子切换、回读身份、保留回退 | 见脚本头部（只能在无人佩戴电极、刺激关闭时使用） | 开机配对核验通过 |
| `sim/run/run_sim.sh`、`run_ssvep_unit_sim.sh`、`check_health_postsynth.sh` | RTL 自检与后综合门 | `sim/README.md` | 6 组自检全部 PASS（`sim/logs/delivered_tree_run/`） |
| `src/tools/ssvep/check_entrypoints.py`、`test_emd1_parse.py` | 原始帧布局唯一来源与解析回归 | 直接运行 | 曾发现离线解析器从错误字节偏移取样的问题 |

## 不适用的情形

这些脚本针对 Zynq UltraScale+ / Vivado 2022.2 / ADS1299 项目写成；`check_names.py`、`check_doc_format.py`、`verify_tar.py`、`board_delete_verified.py` 与具体器件无关，
其余依赖本项目的文件布局与数据包格式，换板子需要改路径和格式常量。
