# data/：公开数据与结果

脑电数据均为公开数据。包内**不含**任何脑电原始数据集（体积大且各有使用条款），只提供获取说明与评估结果；
包内保留的数据只有黄金向量、合成回放文件和评估结果 JSON。

## 公开数据集

| 数据集 | 用途 | 获取 | 放置位置 |
| --- | --- | --- | --- |
| 清华大学 SSVEP 基准数据集（Wang 等，2016，35 人，40 目标） | SSVEP 识别评估（本包使用 S1–S10）、板上自检窗口、SSVEP 单元仿真向量 | http://bci.med.tsinghua.edu.cn/download.html ，每人一个 `S<n>.mat.7z`（约 105 MB），解压得到 `S<n>.mat`；另需 `Freq_Phase.mat`、`Readme.txt` | `data/public/tsinghua2016/mat/S<n>.mat`、`data/public/tsinghua2016/raw/Freq_Phase.mat` |
| Nakanishi 2015 SSVEP（12 目标） | SSVEP 算法正控（`public_dataset_validation.py`） | https://github.com/mnakanishi/12JFPM_SSVEP （`s1.mat`–`s4.mat`） | `data/public/nakanishi2015/` |
| PhysioNet EEG Motor Movement/Imagery（eegmmidb） | 运动想象“抓握 / 静息”基准 | `python src/tools/ssvep/fetch_eegmmidb.py`（从 PhysioNet 官方镜像下载，按官方 `SHA256SUMS.txt` 逐文件校验，ODC-By 1.0） | `data/public/eegmmidb/` |
| MIT-BIH Arrhythmia Database、MIT-BIH Noise Stress Test Database | ECG 流程评估 | PhysioNet（`wfdb` 或网页下载），评估脚本见 `src/tools/ecg/` | `data/public/mitdb/`、`data/public/nstdb/` |

本包使用的清华数据取 8 个后部导联，40 个目标中取 28 个（`round(i*40/28)`），刺激起点后 122 帧（0.4997 s，250 Hz → 244.14 SPS 重采样后）开始计分，
与板上产品的窗口对齐方式一致。

使用这些数据集请遵守各自的引用与许可要求。

## 包内数据

| 路径 | 内容 |
| --- | --- |
| `golden/dsp/` | 滤波 / FFT 的黄金向量（由 `src/tools/dev_scripts/generate_dsp_golden_vectors.py` 生成，Python 测试逐字节比对） |
| `golden/milestone1/` | ADS1299 帧解析与 SPI 的黄金向量（同上，由 `generate_golden_vectors.py` 生成） |
| `replay_synthetic/` | 两个用于回放通路测试的合成录制文件（`internal_test_1025_v1`、`real_dma_event08_v1`）及其说明；内容为板上内部测试信号 / 有限 DMA 导出，不含脑电 |
| `results/ssvep_ps_pl_comparison/` | FBCCA 28 目标准确率与 ITR（`fbcca_28t_off122.json`）、定点对浮点（`fixed_vs_float_q16.json`）、软件基准输出 |
| `results/ssvep_template_study/` | 混合模板的阶段 0 研究：脚本、各窗口起点 / 混合系数的 JSON 与摘要 |
| `results/ssvep_freq_spacing_study.json` | 目标频率间隔研究 |

## 复现实验

```bash
# 数据放好之后（清华 S1–S10）
python src/tools/ssvep/compare_fbcca_etrca.py --json fbcca_28t_off122.json --workers 8
python src/tools/ssvep/pl_fixed_model.py <参数见脚本 --help>
python data/results/ssvep_template_study/phase0.py --help
```
