# 第三方材料与许可证例外

本包的原创代码、脚本与文档采用 MIT 许可证（见 `license.txt`）。下列文件带有自己的许可证，**不受 MIT 覆盖**，按各自条款使用。

| 材料 | 位置 | 许可证 | 说明 |
| --- | --- | --- | --- |
| Linux 内核模块 `eeg_dsp_dma_v2` 及其配套头文件 | `src/driver/`（含 `base_ter3/`） | GPL-2.0（源码头部有 SPDX 行，ioctl / 数据包头文件标注 `GPL-2.0 WITH Linux-syscall-note`，模块声明 `MODULE_LICENSE("GPL")`） | 内核模块必须使用与内核兼容的许可证；用户态程序通过 ioctl 使用，不因此受 GPL 约束 |
| 设备树源文件 | `src/app/dts/`、`sim/milestone_verification/` 下的 `.dts` | GPL-2.0+（文件头标注） | 与内核设备树惯例一致 |
| 板上测试工具 `eeg_dsp_v3_stagetest.c` | `board/tools/` | GPL-2.0-or-later（文件头标注） | 与驱动共用头文件 |
| 板卡厂商的内核驱动（LCD / DSI / HDMI / 摄像头） | `src/system/kernel/vendor/` | GPL-2.0 或 GPL-2.0-or-later（文件头标注，版权属原作者） | 来自开发板厂商的软件包，原样保留，只作系统构建参考，不是本项目原创 |
| 拼音词典（由 Rime `rime-pinyin-simp` 派生） | `src/app/assets/pinyin/`、`src/python_ref/eeg_zynq/operator_v2/resources/` | Apache-2.0（`license_pinyin_simp.txt`、`notice_pinyin_simp.txt`） | 上游源自 Android 开源项目 PinyinIME；修改说明见 `notice_pinyin_simp.txt` |
| Noto Sans CJK 字体 | 字体文件**未随包提供**；许可证文本与来源记录在 `src/app/assets/fonts/` | SIL Open Font License 1.1 | 需要时按 `source.json` 里的地址下载 |
| AMD / Xilinx 工具与 IP（Vivado、Vitis、PetaLinux、AXI DMA、互连等） | 不随包提供；`build/` 与 `src/pl/bd/` 中的脚本只引用它们 | AMD / Xilinx 许可证 | 重建 PL 需要自行安装 Vivado 2022.2 |
| Qt | 不随包提供 | LGPL-3.0 / 商业（取决于使用方式） | 板上使用 Qt 5.15，主机测试使用 Qt 6 |
| 评估用公开数据集 | 不随包提供；获取说明见 `data/README.md` | 各数据集自己的条款（PhysioNet 数据为 ODC-By 1.0 等） | 使用时需遵守各自的引用要求 |
| 厂商底板原理图与引脚总表 | 不随包提供 | 厂商资料 | — |

如发现某个文件的许可证标注与本表不符，以文件头为准，并请告知团队修正。
