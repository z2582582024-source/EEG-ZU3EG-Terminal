`timescale 1ns/1ps

// Physical implementation wrapper used only for the offline unified Vivado
// audit.  It contains no PS, DMA, interrupt controller, ILA, VIO, physical AXI
// base address, or active AXI master.  PL_KEY0 is observed only by a retained
// synchronizer and cannot arm the controller in this wrapper.
module milestone_4d_4i_unified_vivado_top (
    input  wire sys_clk_p,
    input  wire sys_clk_n,
    input  wire pl_key_n,
    input  wire adc1_dout,
    input  wire adc2_dout,
    input  wire adc1_drdy_n,
    input  wire adc2_drdy_n,
    output wire adc_din,
    output wire adc_pwdn_n,
    output wire adc_reset_n,
    output wire adc_sclk,
    output wire adc_start,
    output wire adc1_cs_n,
    output wire adc2_cs_n
);
    wire pl_clk;
    reg [3:0] reset_pipe = 4'b0000;
    wire audit_rst_n = reset_pipe[3];

    (* ASYNC_REG="TRUE", DONT_TOUCH="TRUE" *) reg [1:0] audit_key_sync;
    (* MARK_DEBUG="TRUE", KEEP="TRUE" *) wire audit_key_observed = audit_key_sync[1];

    wire s_axi_awready, s_axi_wready, s_axi_bvalid;
    wire [1:0] s_axi_bresp;
    wire s_axi_arready, s_axi_rvalid;
    (* MARK_DEBUG="TRUE", KEEP="TRUE" *) wire [31:0] s_axi_rdata;
    (* MARK_DEBUG="TRUE", KEEP="TRUE" *) wire [1:0] s_axi_rresp;
    (* MARK_DEBUG="TRUE", KEEP="TRUE" *) wire id_gate_passed;
    (* MARK_DEBUG="TRUE", KEEP="TRUE" *) wire unified_shutdown_safe;
    (* MARK_DEBUG="TRUE", KEEP="TRUE" *) wire unified_fault_safe;
    (* MARK_DEBUG="TRUE", KEEP="TRUE" *) wire [31:0] fifo_occupancy;
    (* MARK_DEBUG="TRUE", KEEP="TRUE" *) wire [4:0] controller_state;

    IBUFDS #(.DIFF_TERM("FALSE"), .IBUF_LOW_PWR("TRUE")) sys_clk_ibufds (
        .I(sys_clk_p), .IB(sys_clk_n), .O(pl_clk));

    always @(posedge pl_clk) reset_pipe <= {reset_pipe[2:0], 1'b1};
    always @(posedge pl_clk or negedge audit_rst_n) begin
        if (!audit_rst_n) audit_key_sync <= 2'b11;
        else audit_key_sync <= {audit_key_sync[0], pl_key_n};
    end

    // The AXI boundary is deliberately inactive.  The retained hierarchy and
    // marked audit nets allow connectivity inspection without pretending that
    // a CPU, PS Block Design, or usable system address exists.
    (* DONT_TOUCH="TRUE", KEEP_HIERARCHY="yes" *)
    milestone_4d_4i_unified_top #(
        .TPOR_WAIT_CYCLES(2_621_440),
        .VCAP_SETTLE_WAIT_CYCLES(20_000_000),
        .RESET_LOW_CYCLES(200),
        .RESET_RELEASE_WAIT_CYCLES(900),
        .SPI_PREPARE_CYCLES(400),
        .SPI_TIMEOUT_CYCLES(100_000),
        .START_ASSERT_CYCLES(800),
        .PARTIAL_TIMEOUT_CYCLES(100_000),
        .DATA_FRAME_TIMEOUT_CYCLES(1_000_000),
        .WATCHDOG_CYCLES(500_000_000),
        .SYS_CLK_HZ(100_000_000),
        .ADS_CLK_HZ(2_048_000),
        .SCLK_HALF_DIV(2),
        .FIFO_DEPTH(4)
    ) dut (
        .acq_clk(pl_clk), .acq_rst_n(audit_rst_n),
        .axi_clk(pl_clk), .axi_rst_n(audit_rst_n),
        .adc1_dout(adc1_dout), .adc2_dout(adc2_dout),
        .adc1_drdy_n(adc1_drdy_n), .adc2_drdy_n(adc2_drdy_n),
        .protocol_fault_in(1'b0), .illegal_cs_fault_in(1'b0),
        .test_data_window_error_in(1'b0),
        .adc_din(adc_din), .adc_pwdn_n(adc_pwdn_n),
        .adc_reset_n(adc_reset_n), .adc_sclk(adc_sclk),
        .adc_start(adc_start), .adc1_cs_n(adc1_cs_n),
        .adc2_cs_n(adc2_cs_n),
        .s_axi_awaddr(9'd0), .s_axi_awvalid(1'b0),
        .s_axi_awready(s_axi_awready), .s_axi_wdata(32'd0),
        .s_axi_wstrb(4'd0), .s_axi_wvalid(1'b0),
        .s_axi_wready(s_axi_wready), .s_axi_bresp(s_axi_bresp),
        .s_axi_bvalid(s_axi_bvalid), .s_axi_bready(1'b1),
        .s_axi_araddr(9'd0), .s_axi_arvalid(1'b0),
        .s_axi_arready(s_axi_arready), .s_axi_rdata(s_axi_rdata),
        .s_axi_rresp(s_axi_rresp), .s_axi_rvalid(s_axi_rvalid),
        .s_axi_rready(1'b1), .id_gate_passed(id_gate_passed),
        .unified_shutdown_safe(unified_shutdown_safe),
        .unified_fault_safe(unified_fault_safe),
        .fifo_occupancy(fifo_occupancy), .controller_state(controller_state)
    );

    // Prevent an unused-input warning from being mistaken for a start path.
    wire _unused_audit_key = audit_key_observed;
endmodule
