# Installed as /lib/modules/$(uname -r)/build/Makefile.
KERNEL_RELEASE := 5.15.36-xilinx-v2022.2-eeg1
KERNEL_SOURCE ?= /usr/src/linux-$(KERNEL_RELEASE)
KERNEL_OUTPUT ?= $(KERNEL_SOURCE)/build

.PHONY: modules modules_install clean prepare
.DEFAULT_GOAL := modules

modules modules_install clean prepare:
	$(MAKE) -C $(KERNEL_SOURCE) O=$(KERNEL_OUTPUT) ARCH=arm64 $@

%:
	$(MAKE) -C $(KERNEL_SOURCE) O=$(KERNEL_OUTPUT) ARCH=arm64 $@
