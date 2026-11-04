"""EXT1 record identity; no mutation of the internal-test EMD1 parser."""
import struct
from eeg_zynq.emd1 import Emd1Record, INTERVAL_NS
class ExternalRecord(Emd1Record):
    def pack(self):
        return struct.pack('<6I16i2I',0x31444D45,0x00600001,self.sequence,
            self.batch_index,self.status1,self.status2,*self.samples,INTERVAL_NS,2)
