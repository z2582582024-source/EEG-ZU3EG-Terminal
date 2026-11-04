# P5B terminal production RTL candidate

Shared 100 MHz `axi_clk` and `acq_clk` are connected to the same board BUFG in
`terminal_board_top.v`. This candidate preserves the P5B physical ADS ports,
control aperture `0x80000000`, DMA aperture `0x80010000`, HP0 noncoherent memory
path and PL IRQ SPI89. `recreate_terminal_bd.tcl` derives from the verified
PS DP ref3 configuration. The build applies the independently verified USB0
delta (`verification/p5b_operator_v2/operator_terminal_01/apply_ps_usb0.tcl`):
ULPI MIO52..63, USB3 GTR1/ref1=26MHz, PHY reset MIO25. Its PS-only evidence
`usb_ps_build_02/output/result.tcldict` preserves the full PL connection/address
signature, DDR/DP/Ethernet and non-USB clocks. MIO24 hub reset belongs to the
device tree; matching FSBL and Linux USB/HID integration remain required.

The explicit build ID is TER2 (`0x16c=0x54455232`) and control ABI is
`0x17c=0x00020002`. The complete software register contract is
`../include/lead_off_terminal.h`.

`INIT_ONLY` (`0x174=1`) starts the permitted ADC clock, executes the existing
three ID reads per device and the EEG register write/readback sequence, and
holds state30 with START low. It does not prepare DMA, enable conversion or
publish raw frames. A lead-off request shares the same command PHY, saves and
restores 19 registers per ADC, disables the test excitation before returning
to ready, and never starts EEG acquisition on its own. An explicit ARM is
accepted only in the live initialized-ready state after RX preparation.
Cancelled/no-status detection may return a failed result with verified EEG
restoration and no core fault; restoration failure retains UNKNOWN.

Normal Stop finishes the current 32-frame chunk, sends the existing ordered
STOP/SDATAC exit, drains FIFO and leaves pins safe. A confirmed-safe platform
reset clears FIFOs, processing state and ADC initialization; repeat use must
run INIT again. A fault/UNKNOWN is not cleared by a software result ACK.

Each 9664-byte EDP2 DMA packet contains 32 unmodified raw EMD1 frames, 16-channel
Q30 filtered values, and up to 256 genuine FFT records. Each of the three
biquad sections uses a five-clock interleaved DSP pipeline; the same channel
returns after16 or more clocks. The filter output tags preserve the source
frame sequence;15 logic clocks of transport do not alter the IIR response.
Channels8..15 use
independent ping-pong sample banks and one time-shared 2048-point FFT. Raw and
filtered samples continue while the FFT computes; full FFT output queues
apply backpressure without replacing actual bins with sentinels. Overflow,
wrong sequence/header or missing timestamp metadata prevents valid decisions.

EDP2 time basis2 stores first/last frame PHY-accept cycles as LE64 at offsets
112 and120. A 64-bit free-running counter advances on the shared100 MHz clock
and resets with the coordinated platform reset. The cycle metadata is stored
with each raw FIFO sequence and checked before packetization. The event is
acceptance of the paired-DRDY frame transfer, with two DOUT streams shifted
in parallel. It is not DMA completion or the ADC sampling aperture. The
nominal digital synchronization delay from the later paired DRDY edge is
3..4 cycles; metastability and analog group delay remain unmeasured. The
conservative accept-to-admission bound is21904 cycles for the fixed DIV50
PHY. CONTROL[3] snapshots current-cycle low/high at0x1a0/0x1a4; Linux brackets
that operation with its monotonic clock instead of timestamping a callback.

Relevant completed offline evidence under `build/p5b_operator_v2/operator_terminal_01`:

- `lead_phy_owner_01/attempt_07`: normal/restoration-fault/INIT_ONLY/cancel/no-status cases.
- `production_contract_04`: real PS AXI boundary, dual ADS pin models, two normal sessions,
  explicit initialize/detect/ARM, FIFO-matched cycle metadata, normal stop and safe reset.
- `dsp_edp2_15`: 16-channel Q30 and exact1025-bin FFT golden comparison, stalls/reset,
  plus a raw ADC-rail vector that remains flagged after filtering.
- `dsp_continuous_04`:5216 raw frames,83456 filtered values and16400 genuine FFT bins
  across two full eight-channel windows while the time-domain stream continues.
- `dsp_faults_02`: magic/version/sequence/TLAST/TKEEP/interval faults close the PL parser.

`build_terminal.tcl --build-terminal-offline NEW_OUTPUT` snapshots the source
closure before running Vivado2022.2. It uses setup/hold, black-box and DRC gates
before generating a bitstream. Simulation/compilation does not prove routed
timing, resource fit, USB enumeration or board operation.
