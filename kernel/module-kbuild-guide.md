# 外置内核模块开发

目标系统安装 eeg_zu3eg_kbuild_devel_v3.tar.gz 到根目录后，源码及生成配置位于：

- /usr/src/linux-5.15.36-xilinx-v2022.2-eeg1
- /usr/src/linux-5.15.36-xilinx-v2022.2-eeg1/build

包同时建立 /lib/modules/5.15.36-xilinx-v2022.2-eeg1/build/Makefile wrapper 及 source 链接。wrapper 把 Kbuild 请求转发给上述源码与生成目录。它已用本项目 DSP 模块在匹配 VM 中编译验证。目标系统的 AArch64 GCC、make、binutils 和 libc 头文件在完整开发 rootfs 内交付。

普通模块直接执行：

    make -C /lib/modules/$(uname -r)/build M="$PWD" modules

模块应先与当前运行 release 匹配，再安装到 /lib/modules/$(uname -r)/extra/ 并运行 depmod -a。这个开发包只用于外置模块，不修改冻结 kernel config，也不要求重新编译整内核。正在运行的采集或 PL owner 活动时不得热换模块。
