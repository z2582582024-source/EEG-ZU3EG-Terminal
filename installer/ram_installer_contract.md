# JTAG RAM 安装器独立入口

`eeg_emmc_ram_install.py` 是 `ram-installer-20261023-01`。冻结的 SD 安装器和绑定字节不改；新入口导入它的载荷、归档、FIT、U-Boot 脚本、MMC 枚举、所有权和逐文件读回校验函数。不能用旧 `burn-emmc.sh` 代替新入口。

## 来源与内存

运行根必须为 rootfs/tmpfs/ramfs，cmdline 必须含 `eeg_jtag_install=1`。`/etc/eeg-jtag-installer.json` 的 schema 为 `eeg.zu3eg.jtag_environment.v1`；绑定 product、revision、kernel_release、四项 system_identity、payload_manifest_sha256、platform_xsa_sha256、installer_sha256、validator_sha256。plan 还绑定本次 `/proc/sys/kernel/random/boot_id`。

HTTP transport 是独立映射，不改历史 payload manifest。源为唯一 final_sd_02 四载荷；不读取已退役独立 ext4。每段 offset/length/SHA256、最终 length/SHA256 来自 transport；transport 本身与 payload manifest 各有独立 SHA pin。HTTP 必须精确返回 206/Content-Range，已缓存分段先重新计算 SHA 再续传。不会默认关闭校验，不将目标盘当缓存。

默认缓存 `/run/eeg-install/payload`，约 1.083 GB 压缩载荷；以实机 MemAvailable 和 tmpfs 可用空间双门禁，保留 512 MiB 内核、Python/归档审计余量。实际匹配 DT 仅宣告 2 GiB，不能按物理 4 GiB放行。rootfs 解压直接写已确认 eMMC，绝不在 RAM 展开 3.2 GB。RAM 峰值仍须板测；门禁不满足时停在 fetch，不更改内核或省掉功能。

## Host 服务契约

- `GET /payload/<canonical-file>` 支持精确 Range。transport schema `eeg.zu3eg.http_transport.v1`，包含 payload_manifest_sha256、payload_manifest_bytes、chunk_bytes、artifacts；每个 artifact 含 role/path/bytes/sha256/chunks，每 chunk 含 offset/bytes/sha256。
- host_config 可选 `assets=[{name,path,bytes,sha256}]`，例如 application_bundle、system_tools_delta。仅配置的绝对普通文件在 State 启动时完整核 SHA/长度后，经 `GET /assets/<name>` 只读提供完整或 Range；名称不可含路径，无法列表或通过 HTTP 动态添加。它不改变原四载荷 transport，也不由 eMMC worker 隐式安装。新增应用/系统工具仍须各自验证、明确部署与版本回执；运行中的安装服务不得中途换代码或配置。
- `GET /v1/journal/target/{CID}` 返回 `attempts`。任何非 terminal 旧尝试阻止新写入；已用 attempt 也不重复执行。
- `POST /v1/journal/{attempt}/{sequence:06d}` 接受 canonical UTF-8 JSON：schema `eeg.zu3eg.ram_journal_event.v1`、attempt、sequence、prev_sha256、target_cid、phase、action、payload。host 必须持久 fsync 后回 `{accepted:true,sha256:<exact-body-sha>,sequence:N}`。相同 bytes 可幂等，冲突 bytes 拒绝。没有精确 ACK 就不执行下一条目标命令。
- 成功 phase `READBACK_VERIFIED_BOOT_PENDING`；故障 `FAILED_UNKNOWN` 非 terminal，禁止自动再次分区/格式化/安装。`REJECTED_BEFORE_WRITE` 只用于尚未开始目标写入。
- 备份每个用户区/boot0/boot1 采用独立 uuidhex id。`POST /v1/backup/{id}/begin` 接受 schema `eeg.zu3eg.emmc_backup.v1`、target_cid、size_bytes、chunk_bytes=4194304、area。`GET .../status` 返回同样四项身份与 chunks[{sequence,sha256,bytes}]。客户端逐段只读目标，PUT `.../chunks/{sequence:06d}` 原始 bytes，头 X-Content-SHA256；host 持久回 accepted/sequence/sha256/bytes。最后 POST `.../complete` {sha256,size_bytes}，host 按序完整重新读取备份分段并回 verified/sha256/bytes/backup_location。host 保留真实 chunk 文件，SHA 不能替代备份。

## plan、确认与 apply

plan 唯一选择 type=MMC、non-removable、准确 CID 的用户区；不假设 mmcblk0/1。检查完整 rootfs 容量、挂载、swap、holders、工具和真实维护检查。`eeg-maintenance-check` 缺 `/dev/eeg-dsp` 或实际 ioctl 失败就是 UNKNOWN；不伪造静态 IDLE。

apply 需要新的 `eeg.zu3eg.emmc_ram_consent.v1` JSON：attempt（绑定本次唯一 uuidhex）、target_cid、user_bytes、plan_sha256、ram_boot_id、operator_confirmed_erase=true、adc_isolated=true、overwrite 精确复制 plan；recovery 为 route=jtag_ram、environment_sha256、board_boot_receipt_sha256。本轮现场确认由操作者提供，代码不能自动生成同意。新 attempt 不得复用旧同意。

backup_decision 为 `backup_completed`，areas 放 host 真实完成回执并加 backup_id/area/target_cid；apply 再读 host status 核 verified/sha256/size_bytes/target_cid。或操作者明确 `no_unique_data_confirmed`、operator_confirmed_no_unique_data=true 和 reason。任何 `sd_rescue_retained=true` 单独布尔均不能满足新契约。

写入仅 partition table、FAT BOOT、ext4 rootfs 用户区；boot0/boot1/RPMB/EXT_CSD/QSPI 排除。worker 在首写前复核实际目标/无占用/真实 IDLE，每条命令前先持久事件；不自动重试目标动作。BOOT、FIT、脚本、manifest、全归档文件/权限/owner/链接/设备节点、UUID/PARTUUID/fstab、开发必需路径和 flush 后只读回读均验证。RAM 临时 shadow/SSH keys/网络状态不复制到日常系统；首次密码继续由串口隐藏设置。

成功结果仍是 `READBACK_VERIFIED_BOOT_PENDING`。它不等于 eMMC BootROM 或独立启动 PASS；必须现场移除 SD 回退、切准确模式、脱离 JTAG RAM，新启动并核 root/PARTUUID/日志与受控重启持久性。

## 离线复测与恢复边界

从产品根运行：`py -3 verification/eeg_zu3eg_baseline/installer/run_ram_installer_offline_regression.py`。该测试包含真实生产 worker 调度与临时文件，设备/外部命令 I/O 为模拟；不访问任何物理介质，不替代板测。

安装中断后先重入同一匹配 JTAG RAM 环境，只做 plan 和 host journal/内容观察。非 terminal 旧 attempt 保留；不能删除 marker/journal 清 UNKNOWN。由实际观察选择新安装或按 host 分段备份恢复，并重新展示具体 CID/范围/备份和取得覆盖确认。恢复写操作不在此入口中自动执行，避免复用旧安装同意。

生成后没有用的文件记得删除，不要堆积，删除标准和以前一样。本实现只新增源码、定向验证和小型回执；未复制整盘或 rootfs，未删除任何旧资产。
