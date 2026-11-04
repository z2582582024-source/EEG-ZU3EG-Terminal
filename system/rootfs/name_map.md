# rootfs name map

One file of the root file system overlay is stored under a repository-safe name:

| file in this repository | install path on the board |
| --- | --- |
| `etc/systemd/system/serial_getty_ttyps0_eeg.conf` | `/etc/systemd/system/serial-getty@ttyPS0.service.d/eeg.conf` |
