`timescale 1ns/1ps

// Independent Milestone 4D top.  The 11 frozen ADS I/O pins are retained.
// software_shutdown_req and test-data validation remain internal hooks until
// the replacement-U1 DUAL-ID-DIAG gate is passed; both are tied safe here.
module milestone_4d_internal_test_top (
    input  wire sys_clk_p, input wire sys_clk_n, input wire pl_key_n,
    input  wire adc1_dout, input wire adc2_dout,
    input  wire adc1_drdy_n, input wire adc2_drdy_n,
    output wire adc_din, output wire adc_pwdn_n, output wire adc_reset_n,
    output wire adc_sclk, output wire adc_start,
    output wire adc1_cs_n, output wire adc2_cs_n
);
    wire pl_clk;
    reg [3:0] reset_pipe = 4'b0000;
    wire ctrl_rst_n = reset_pipe[3];
    wire [3:0] raw_inputs = {adc2_dout, adc1_dout, adc2_drdy_n, adc1_drdy_n};
    wire [4:0] state; wire [31:0] counter; wire [7:0] error_flags;
    wire done, shutdown_safe, fault_safe, key_sync, key_armed, key_event;
    wire [1:0] accepted_count; wire [3:0] sync_inputs; wire [6:0] control_outputs;
    wire adc_din_i, adc_pwdn_n_i, adc_reset_n_i, adc_sclk_i, adc_start_i;
    wire adc1_cs_n_i, adc2_cs_n_i, spi_busy, spi_done, spi_start;
    wire [2:0] spi_operation; wire [8:0] spi_transfer_bits;
    wire [7:0] ads1_id_raw, ads2_id_raw; wire [1:0] id_valid;
    wire [23:0] ads1_id_reads, ads2_id_reads; wire [7:0] id_diag_flags;
    wire [7:0] ads1_config_readback, ads2_config_readback;
    wire [3:0] id_summary, config_status; wire [2:0] test_window_status;
    wire shutdown_req_seen; wire [7:0] transport_error_flags, register_error_flags, test_error_flags;

    IBUFDS #(.DIFF_TERM("FALSE"), .IBUF_LOW_PWR("TRUE")) sys_clk_ibufds (
        .I(sys_clk_p), .IB(sys_clk_n), .O(pl_clk));
    always @(posedge pl_clk) reset_pipe <= {reset_pipe[2:0], 1'b1};

    (* DONT_TOUCH = "TRUE" *) milestone_4d_internal_test_ctrl #(
        // Safety fallback only.  The production integration must issue the
        // software request before this five-second watchdog expires.
        .TEST_WINDOW_CYCLES(32'd500_000_000)
    ) ctrl (
        .clk(pl_clk), .rst_n(ctrl_rst_n), .key_n(pl_key_n), .raw_inputs(raw_inputs),
        .software_shutdown_req(1'b0), .test_data_window_error(1'b0),
        .protocol_fault_in(1'b0), .state(state), .counter(counter),
        .error_flags(error_flags), .done(done), .shutdown_safe(shutdown_safe),
        .fault_safe(fault_safe), .key_synchronized(key_sync), .key_armed(key_armed),
        .key_press_event(key_event), .accepted_event_count(accepted_count),
        .synchronized_inputs(sync_inputs), .control_outputs(control_outputs),
        .adc_din(adc_din_i), .adc_pwdn_n(adc_pwdn_n_i), .adc_reset_n(adc_reset_n_i),
        .adc_sclk(adc_sclk_i), .adc_start(adc_start_i), .adc1_cs_n(adc1_cs_n_i),
        .adc2_cs_n(adc2_cs_n_i), .spi_busy(spi_busy), .spi_done(spi_done),
        .spi_start(spi_start), .spi_operation(spi_operation),
        .spi_transfer_bits(spi_transfer_bits), .ads1_id_raw(ads1_id_raw),
        .ads2_id_raw(ads2_id_raw), .id_valid(id_valid), .ads1_id_reads(ads1_id_reads),
        .ads2_id_reads(ads2_id_reads), .id_diagnostic_flags(id_diag_flags),
        .id_summary(id_summary), .ads1_config_readback(ads1_config_readback),
        .ads2_config_readback(ads2_config_readback),
        .config_status(config_status), .test_window_status(test_window_status),
        .shutdown_req_seen(shutdown_req_seen), .transport_error_flags(transport_error_flags),
        .register_error_flags(register_error_flags), .test_error_flags(test_error_flags));

    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_din(.I(adc_din_i),.O(adc_din));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_pwdn_n(.I(adc_pwdn_n_i),.O(adc_pwdn_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_reset_n(.I(adc_reset_n_i),.O(adc_reset_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_sclk(.I(adc_sclk_i),.O(adc_sclk));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_start(.I(adc_start_i),.O(adc_start));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc1_cs_n(.I(adc1_cs_n_i),.O(adc1_cs_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc2_cs_n(.I(adc2_cs_n_i),.O(adc2_cs_n));

    // Probe aliases are explicit so future CSV/LTX exports retain number and
    // meaning.  Probes 0..24 mirror DUAL-ID-DIAG; 25..31 are 4D-only.
    (* KEEP = "TRUE" *) wire [4:0] ila_probe00_state = state;
    (* KEEP = "TRUE" *) wire [31:0] ila_probe01_counter = counter;
    (* KEEP = "TRUE" *) wire [6:0] ila_probe02_control_outputs = control_outputs;
    (* KEEP = "TRUE" *) wire [1:0] ila_probe03_pwdn_reset = {adc_pwdn_n_i,adc_reset_n_i};
    (* KEEP = "TRUE" *) wire [7:0] ila_probe04_error_flags = error_flags;
    (* KEEP = "TRUE" *) wire ila_probe05_done_test_window = done;
    (* KEEP = "TRUE" *) wire ila_probe06_shutdown_safe = shutdown_safe;
    (* KEEP = "TRUE" *) wire ila_probe07_fault_safe = fault_safe;
    (* KEEP = "TRUE" *) wire [3:0] ila_probe08_synchronized_inputs = sync_inputs;
    (* KEEP = "TRUE" *) wire [3:0] ila_probe09_raw_inputs = raw_inputs;
    (* KEEP = "TRUE" *) wire ila_probe10_pl_key_n = pl_key_n;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe11_key_status = {key_sync,key_armed,key_event};
    (* KEEP = "TRUE" *) wire [1:0] ila_probe12_accepted_event_count = accepted_count;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe13_spi_operation = spi_operation;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe14_spi_handshake = {spi_busy,spi_done,spi_start};
    (* KEEP = "TRUE" *) wire [1:0] ila_probe15_chip_selects = {adc1_cs_n_i,adc2_cs_n_i};
    (* KEEP = "TRUE" *) wire [7:0] ila_probe16_ads1_id_raw = ads1_id_raw;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe17_ads2_id_raw = ads2_id_raw;
    (* KEEP = "TRUE" *) wire [1:0] ila_probe18_id_valid = id_valid;
    (* KEEP = "TRUE" *) wire [8:0] ila_probe19_spi_transfer_bits = spi_transfer_bits;
    (* KEEP = "TRUE" *) wire [23:0] ila_probe20_ads1_id_reads = ads1_id_reads;
    (* KEEP = "TRUE" *) wire [23:0] ila_probe21_ads2_id_reads = ads2_id_reads;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe22_id_read_index =
        ((state == 5'd7)  || (state == 5'd11)) ? 3'd1 :
        ((state == 5'd8)  || (state == 5'd12)) ? 3'd2 :
        ((state == 5'd9)  || (state == 5'd13)) ? 3'd3 :
        3'd0;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe23_id_diagnostic_flags = id_diag_flags;
    (* KEEP = "TRUE" *) wire [3:0] ila_probe24_id_summary = id_summary;
    (* KEEP = "TRUE" *) wire [3:0] ila_probe25_config_status = config_status;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe26_test_window_status = test_window_status;
    (* KEEP = "TRUE" *) wire [15:0] ila_probe27_config_readback =
        {ads2_config_readback,ads1_config_readback};
    (* KEEP = "TRUE" *) wire ila_probe28_software_shutdown_req = 1'b0;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe29_transport_error_flags = transport_error_flags;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe30_register_error_flags = register_error_flags;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe31_test_error_flags = test_error_flags;

    ila_m4d_internal_test ila_i(.clk(pl_clk),
        .probe0(ila_probe00_state),.probe1(ila_probe01_counter),.probe2(ila_probe02_control_outputs),
        .probe3(ila_probe03_pwdn_reset),.probe4(ila_probe04_error_flags),.probe5(ila_probe05_done_test_window),
        .probe6(ila_probe06_shutdown_safe),.probe7(ila_probe07_fault_safe),.probe8(ila_probe08_synchronized_inputs),
        .probe9(ila_probe09_raw_inputs),.probe10(ila_probe10_pl_key_n),.probe11(ila_probe11_key_status),
        .probe12(ila_probe12_accepted_event_count),.probe13(ila_probe13_spi_operation),.probe14(ila_probe14_spi_handshake),
        .probe15(ila_probe15_chip_selects),.probe16(ila_probe16_ads1_id_raw),.probe17(ila_probe17_ads2_id_raw),
        .probe18(ila_probe18_id_valid),.probe19(ila_probe19_spi_transfer_bits),.probe20(ila_probe20_ads1_id_reads),
        .probe21(ila_probe21_ads2_id_reads),.probe22(ila_probe22_id_read_index),.probe23(ila_probe23_id_diagnostic_flags),
        .probe24(ila_probe24_id_summary),.probe25(ila_probe25_config_status),.probe26(ila_probe26_test_window_status),
        .probe27(ila_probe27_config_readback),.probe28(ila_probe28_software_shutdown_req),
        .probe29(ila_probe29_transport_error_flags),.probe30(ila_probe30_register_error_flags),
        .probe31(ila_probe31_test_error_flags));
endmodule
