"""Detach all bound EEG PL MMIO drivers before PCAP, restore only after operating."""
from pathlib import Path
import subprocess
import struct

class LifecycleError(RuntimeError):
    pass

class PlDriverLifecycle:
    def __init__(self, platform_root=Path('/sys/bus/platform'), module_root=Path('/sys/module'),
                 run=None, event=None):
        self.platform=platform_root;self.modules=module_root
        self.run=run or self._run;self.event=event or (lambda name,detail:None)
        self.dma_name='80010000.dma-controller';self.driver='xilinx-vdma'
        self.retired=False
    @staticmethod
    def _run(argv):
        subprocess.run(argv,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
    def retire(self):
        device=self.platform/'devices'/self.dma_name
        driver=self.platform/'drivers'/self.driver
        # A previous restore may have rebound only the DMA provider before
        # client probing failed. Retire that partial state before rollback.
        if self.retired and not (device/'driver').exists() and not (self.modules/'eeg_dsp_dma_v2').exists():return
        if not device.is_dir() or not driver.is_dir():raise LifecycleError('EXACT_PL_DMA_DEVICE_DRIVER_REQUIRED')
        for other in (self.platform/'devices').iterdir():
            prop=other/'of_node/reg'
            if other.name in (self.dma_name,'80000000.eeg') or not (other/'driver').is_symlink() or not prop.is_file():continue
            raw=prop.read_bytes()
            if len(raw)>=16:
                hi,lo=struct.unpack_from('>2I',raw);address=(hi<<32)|lo
                if 0x80000000<=address<0xc0000000:
                    raise LifecycleError('OTHER_BOUND_PL_MMIO_DRIVER:'+other.name)
        link=device/'driver'
        if not link.is_symlink() or link.resolve()!=driver.resolve():raise LifecycleError('PL_DMA_BINDING_IDENTITY_UNPROVEN')
        # Every client fd/worker/DMA/IRQ pending condition has already been
        # rejected by the production maintenance gate. No forced rmmod exists.
        if (self.modules/'eeg_dsp_dma_v2').exists():
            self.event('CLIENT_UNLOAD_REQUEST','eeg_dsp_dma_v2')
            self.run(['rmmod','eeg_dsp_dma_v2'])
        elif not self.retired:raise LifecycleError('EXPECTED_CAPTURE_CLIENT_NOT_LOADED')
        if (self.modules/'eeg_dsp_dma_v2').exists():raise LifecycleError('PL_CLIENT_UNLOAD_NOT_CONFIRMED')
        self.event('DMA_PROVIDER_UNBIND_REQUEST',self.dma_name)
        (driver/'unbind').write_text(self.dma_name+'\n',encoding='ascii')
        if link.exists():raise LifecycleError('PL_DMA_PROVIDER_UNBIND_NOT_CONFIRMED')
        self.retired=True;self.event('PL_MMIO_DRIVERS_RETIRED','client and DMA provider unbound before firmware write')
    def restore(self):
        if not self.retired:raise LifecycleError('PL_DRIVER_RETIREMENT_NOT_RECORDED')
        device=self.platform/'devices'/self.dma_name;driver=self.platform/'drivers'/self.driver
        self.event('DMA_PROVIDER_BIND_REQUEST',self.dma_name)
        (driver/'bind').write_text(self.dma_name+'\n',encoding='ascii')
        if not (device/'driver').is_symlink() or (device/'driver').resolve()!=driver.resolve():
            raise LifecycleError('PL_DMA_PROVIDER_REBIND_NOT_CONFIRMED')
        self.event('CLIENT_LOAD_REQUEST','eeg_dsp_dma_v2')
        self.run(['modprobe','eeg_dsp_dma_v2'])
        if not (self.modules/'eeg_dsp_dma_v2').is_dir():raise LifecycleError('PL_CLIENT_LOAD_NOT_CONFIRMED')
        self.retired=False;self.event('PL_MMIO_DRIVERS_RESTORED','DMA provider then capture client')
