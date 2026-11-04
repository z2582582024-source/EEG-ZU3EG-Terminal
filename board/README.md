# board/：板级资料、板上工具与实测输出

## 硬件

| 项 | 内容 |
| --- | --- |
| 主控板 | MPSoC P5B 开发板，AMD XCZU3EG-SFVC784-2-I：8 GB eMMC、64 MB QSPI、PS DDR4 4 GB、PL DDR4 1 GB、PS-GTR USB 3.0、MiniDP、M.2 |
| 采集板 | 自制 16 通道脑电采集板：两片 ADS1299，经 FMC 子板（电平转换）与 ZU3EG PL 的 HP Bank（1.8 V）连接，有线 SPI |
| 公共信号 | `ADC_SCLK`、`ADC_DIN`、`ADC_RESET_N`、`ADC_PWDN_N`、`ADC_START`；每片独立：`ADCx_CS_N`、`ADCx_DOUT`、`ADCx_DRDY_N` |
| 脑电帽映射 | CH01–CH08（ADS1299-1）= FC3、FC4、C3、Cz、C4、CP3、CPz、CP4；CH09–CH16（ADS1299-2）= PO7、PO3、POz、PO4、PO8、O1、Oz、O2；REF/SRB1 = FCz；BIAS = AFz |
| ECG 实验模式 | 14 路头皮 EEG + 2 路胸部差分 ECG（同一片 ADS1299 的两个空闲通道，增益相同，不加入 BIAS 感测） |

| 文件 | 内容 |
| --- | --- |
| `hardware/pin_constraints_zynq_ads1299.xlsx` | ADS1299 与 Zynq PL 的引脚映射（已在 Vivado 中验证，最终约束 `src/pl/constraints/terminal_board.xdc` 与其一致） |
| `hardware/electrode_to_ads1299_pin_map.xlsx` | 脑电线标签、ADS1299 物理输入、软件逻辑通道三套编号的对应表 |
| `hardware/eeg_cap_16ch_mapping.md` | 16 通道固定电极映射 |
| `hardware/acquisition_board/eeg16_acquisition_board_2026_09_03.epro2` | 采集板的嘉立创 EDA 工程文件 |

主控板厂商的底板原理图与引脚总表是厂商资料，没有随包提供；PL 引脚按上表与 XDC 已核对。

## 板上工具（`tools/`）

| 文件 | 作用 |
| --- | --- |
| `eeg_dsp_v3_stagetest.c` | 驱动的分阶段测试：初始化 → 接触诊断 → 开始 → 数据包 → 状态计数 → 停止 → 复位，逐步打印状态与健康块；用法 `eeg_dsp_v3_stagetest <输出前缀> <会话数> <包数> [增益,…]`；输出样例见 `measured/st359.log` |
| `ssvep_ops_replay.c` | 把仿真用的同一份 SSVEP 单元操作程序（`sim/golden/gen_unit_vectors.py` 生成的 `ops.hex`）通过驱动 ioctl 在真实 PL 上回放；通过 = 硬件逐位复现黄金模型（参数 CRC、窗口对齐、各检查点得分、带 CRC 的判决记录、捕获 RAM、延迟计数）。用法 `ssvep_ops_replay ops.hex [/dev/eeg-dsp] [--stats]`；上板回放 112,345 次操作、0 错误 |

两个程序都需要在板上（或用 aarch64 交叉工具链）编译，并通过 `/dev/eeg-dsp` 字符设备访问驱动；运行它们会启动采集或访问 PL 窗口，适用同样的安全约束。

## 实测输出（`measured/`）

| 文件 | 内容 |
| --- | --- |
| `etrca_selftest_pair358.txt` | 板上 eTRCA 自检（公开数据窗口经 PL 馈入）：84 窗口，82 正确，1 错误，1 无判决；PL 检查点延迟均值 1,890 µs，单帧 848 µs |
| `pair359/etrca_selftest_hang01_pair359.txt` | 加固后的配对和应用上重复的同一自检，结果一致（82/84） |
| `pair359/compare_hw_to_golden.txt` | 153 个数据包、654,687 字与黄金模型逐字比对：无不一致段，频谱 12,296 条记录 0 不一致 |
| `pair359/compare_tap_capture.txt` | SSVEP 抽头抓取的 800 帧与黄金抽取器（起点对齐）逐帧相同；起点偏 1–N 帧时只有部分相同，说明对齐判据有效 |
| `pair359/st359.log` | 一次分阶段会话（153 个数据包）：初始化 → 接触诊断 → 采集 → 复位，状态计数全程 error = 0、dropped = 0 |
| `pair359/longrun_scope_11min_monitor.log` | 11 分钟连续采集的状态监测：5,005 个数据包，丢包 0，错误 0 |
| `st357.log`、`st357b.log`、`st_tap.log` | 早期分阶段会话日志 |

这些日志都是在**没有人佩戴电极、刺激源关闭**的条件下得到的（输入为板上自检信号或回放）；真人在线闭环另行测试，效果良好，不在这些日志的范围内。

## 台式示波器实测（`measured/bench_scope/`）

2026-10-11 用 Rigol DS2072A 拍摄的三张照片：通道 1 为 PL 产生的 ADC_CLK，周期 500 ns、频率 2.000 MHz（见 `scope_20261011_1645.jpg` 的测量栏）；另两张（`scope_20261011_1945.jpg`、`scope_20261011_2047.jpg`）为同日的时钟与另一路信号的边沿关系（100 ns / 格）。这是 4C 阶段 ADS1299 ID 读通（两片各三次 `0x3E`）之后的时钟实测。

## 在板上部署（概述）

> 以下操作会重新装载 PL 与内核模块。**只能在无人佩戴电极帽、屏幕刺激关闭时进行。**

1. **位流 + 驱动模块成对发布**：位流（TER5）与驱动模块（`eeg_dsp_dma_v2.ko`）必须成对，配对清单 `build/artifacts/pair_manifest.json`；`build/pair/ter5_pair_update.py` 完成先备份、再原子切换、再回读身份的更新，并保留上一版作回退。
   位流与模块不配套时（例如新应用访问旧模块没有的 SSVEP 窗口）曾导致整机挂死，所以配对检查是强制的。
2. **开机默认**：`/usr/lib/eeg-system/ter3-pair` 先装 TER3、再装 TER5（脚本见 `build/pair/boot/`），开机日志里有两次 `PAIR_LOADED_IDENTITY_VERIFIED`，回执写入 `/opt/eeg/.../receipts`。
3. **应用**：Qt 终端在板上原生编译（Qt 5.15，`-Werror`），按版本目录安装，`eeg-app-update` 在 `current` / `previous` 之间切换，可一键回退。
4. **系统与镜像**：`src/system` 含 eMMC 安装器、根文件系统覆盖、设备树、启动脚本与 JTAG 引导工具（Windows 主机端）。

## 安全约束（开发中形成的规则）

* 正常停止后 PL 数据通路处于复位，对 PL 窗口的 MMIO 访问会使 CPU 挂起；任何 PL 访问都要先检查驱动状态。
* 任何涉及装载 PL / 模块、重启的操作前，确认无人佩戴电极、刺激源关闭。
* 设备故障会被锁存，只能靠重启清除；终端在故障后显示倒计时并自动重启（15 分钟内最多 2 次，避免循环重启）。这是缓解措施，不是根因修复。
* 删除板上数据按“先拷到主机、逐文件校验 SHA、再删除、记入台账”的规程。
