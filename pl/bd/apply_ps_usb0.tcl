# P5B base v1.2/core v1.1 USB0 host physical map, not a board preset.
# Source is inert. Caller owns a new Vivado 2022.2 candidate and records hashes.
namespace eval ::p5b_usb0 {}
proc ::p5b_usb0::apply {ps} {
    if {[version -short] ne "2022.2"} {error MATCHED_VIVADO_2022_2_REQUIRED}
    set requested [list \
        CONFIG.PSU__USB0__PERIPHERAL__ENABLE 1 \
        CONFIG.PSU__USB0__PERIPHERAL__IO {MIO 52 .. 63} \
        CONFIG.PSU__USB3_0__PERIPHERAL__ENABLE 1 \
        CONFIG.PSU__USB3_0__PERIPHERAL__IO {GT Lane1} \
        CONFIG.PSU__USB0__REF_CLK_SEL {Ref Clk1} \
        CONFIG.PSU__USB0__REF_CLK_FREQ 26 \
        CONFIG.PSU__USB0__RESET__ENABLE 1 \
        CONFIG.PSU__USB0__RESET__IO {MIO 25} \
        CONFIG.PSU__USB__RESET__MODE {Shared MIO Pin} \
        CONFIG.PSU__USB__RESET__POLARITY {Active Low} \
        CONFIG.PSU__CRL_APB__USB0_BUS_REF_CTRL__DIVISOR0 4 \
        CONFIG.PSU__CRL_APB__USB0_BUS_REF_CTRL__DIVISOR1 1 \
        CONFIG.PSU__CRL_APB__USB3_DUAL_REF_CTRL__DIVISOR0 5 \
        CONFIG.PSU__CRL_APB__USB3_DUAL_REF_CTRL__DIVISOR1 10]
    foreach {key value} $requested {
        if {[lsearch -exact [list_property $ps] $key] < 0} {error "USB_PROPERTY_REQUIRED:$key"}
    }
    set_property -dict $requested $ps
    validate_bd_design
    foreach {key value} $requested {
        if {[get_property $key $ps] ne $value} {error "USB_READBACK_CONFLICT:$key"}
    }
    # Preserve the existing shared IOPLL (~999.99MHz). Disabled-USB readbacks
    # were stale nominal defaults (250/20); real enabled outputs use /4 and
    # /50, approximately 249.9975 and 19.9998MHz without retuning any PLL.
    foreach {key low high} {CONFIG.PSU__CRL_APB__USB0_BUS_REF_CTRL__ACT_FREQMHZ 249 250 CONFIG.PSU__CRL_APB__USB3_DUAL_REF_CTRL__ACT_FREQMHZ 19.9 20} {
        set actual [get_property $key $ps]
        if {![string is double -strict $actual] || $actual < $low || $actual > $high} {
            error "USB_ENABLED_CLOCK_OUTSIDE_BOUND:$key:$actual"
        }
    }
    # Do not expose additional EMIO hub ports. The external GL3523T hub is
    # on MIO24 reset and its own current-limit circuitry in the board schematic.
    foreach key {CONFIG.PSU__USB1__PERIPHERAL__ENABLE CONFIG.PSU__USB3_1__PERIPHERAL__ENABLE CONFIG.PSU__USE__USB3_0_HUB CONFIG.PSU__USB3_0__EMIO__ENABLE} {
        if {[get_property $key $ps] != 0} {error "UNREQUESTED_USB_OR_EMIO_PATH:$key"}
    }
    return $requested
}
