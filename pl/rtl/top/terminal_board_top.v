// Physical P5B clock and ADS ports only. All control permissions are internal.
// PL_KEY0 is deliberately absent: AXI CONTROL owns the unchanged start entry.
module terminal_board_top(
    input wire sys_clk_p,sys_clk_n,
    input wire adc1_dout,adc2_dout,adc1_drdy_n,adc2_drdy_n,
    output wire adc_clk,adc_din,adc_pwdn_n,adc_reset_n,adc_sclk,
    output wire adc_start,adc1_cs_n,adc2_cs_n
);
    wire raw_clock,shared_clock;
    IBUFDS #(.DIFF_TERM("FALSE"),.IBUF_LOW_PWR("TRUE")) board_clock(
        .I(sys_clk_p),.IB(sys_clk_n),.O(raw_clock));
    BUFG shared_buffer(.I(raw_clock),.O(shared_clock));
    normal_ps_axi_wrapper platform(
        .axi_clk(shared_clock),.acq_clk(shared_clock),
        .adc1_dout(adc1_dout),.adc2_dout(adc2_dout),
        .adc1_drdy_n(adc1_drdy_n),.adc2_drdy_n(adc2_drdy_n),
        .adc_clk(adc_clk),.adc_din(adc_din),.adc_pwdn_n(adc_pwdn_n),
        .adc_reset_n(adc_reset_n),.adc_sclk(adc_sclk),.adc_start(adc_start),
        .adc1_cs_n(adc1_cs_n),.adc2_cs_n(adc2_cs_n));
endmodule
