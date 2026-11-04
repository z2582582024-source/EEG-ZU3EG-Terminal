# eMMC Installer

`burn-emmc.sh` packages to `/usr/lib/eeg-system/eeg_emmc_install.py` and defaults to `plan`. The plan reads the manifest from `/var/lib/eeg-system/install-payload/current/system_payload.manifest.json`, hashes the complete BOOT/FIT/script/rootfs role set, audits the rootfs tar and active-mmc PARTUUID boot script, identifies SD root and eMMC by MMC type/CID/capacity, and shows blockers. A blocked plan exits 3. It does not write eMMC or auto-select a target by mmc index.

`system_payload.manifest.json` and every role artifact must live beneath the same directory on the SD ext4 root. Keep the large rootfs tar off the FAT32 BOOT partition. The SD and installed eMMC use the same complete FIT, boot script and rootfs. The installer creates a new DOS user-area partition table with 512 MiB FAT BOOT and an ext4 development root. It does not write boot0, boot1, RPMB, EXT_CSD or QSPI.

The rootfs manifest must state `rootfs.init_system` as `systemd` or `sysv`. Validation resolves `/sbin/init` within the archive and requires it to point to the declared PID 1 implementation. It also checks the matching serial-login and service startup/stop hooks; for systemd this includes enabled sshd, EEG service and `ttyPS0` getty links. A marker file or an unused `inittab` is not treated as a boot service.

The installer refuses Apply until the booted SD system matches manifest kernel/ABI, the target is a separate non-removable MMC device, every artifact and rootfs path/hash/identity is valid, enough capacity and development free space remain, no target partition is mounted or in use, `/run/eeg-operator/maintenance.lock` can be held exclusively, the live maintenance UAPI reports acquisition low/capture idle/stimulus off/DMA halted/IRQ quiesced/owner released, and the operator supplies `--adc-isolated`. The software cannot observe physical ADC power/isolation. This explicit physical confirmation is only for this installation.

Before Apply, back up any unique eMMC data, keep a verified bootable SD rescue card, and create a local decision record on the SD with the exact target CID:

```json
{
  "schema": "eeg.zu3eg.emmc_backup_decision.v1",
  "target_cid": "exact 32-hex-character CID from plan",
  "decision": "backup_completed",
  "backup_location": "operator's recorded backup location",
  "sd_rescue_retained": true,
  "covers": ["emmc_user_area", "emmc_boot0", "emmc_boot1"]
}
```

After independently checking the plan output and backup record, Apply takes the actual target CID twice to bind the write to that device:

```sh
/opt/image/burn-emmc.sh plan
/opt/image/burn-emmc.sh apply --target-cid '<CID>' --confirm-erase '<CID>' \
  --backup-record /var/lib/eeg-system/emmc-backup-decision.json --adc-isolated
```

The installer writes only the target's user area, checks boot file hashes before and after unmount, audits ext4 read-only, and records a local receipt under `/var/log/eeg/`. It never reboots. A successful write receipt says `EMMC_BOOT_PENDING`, not boot verified. To verify the eMMC boot path, shut down normally, remove the SD, select the board's eMMC boot mode, power on, then verify `/proc/cmdline` root `PARTUUID`, `/etc/eeg-system/baseline.json`, SSH, application and persistent storage. Keep the SD rescue unchanged. If a write fails, preserve its receipt and boot from the rescue SD; do not rerun Apply until the recorded target state and filesystems have been re-inspected.

Host-only validator: `verification/eeg_zu3eg_baseline/installer/check_payload.py --manifest <manifest>`. Host-only tests: `py -3.11 verification/eeg_zu3eg_baseline/installer/test_eeg_emmc_install.py`. Neither writes physical media.
