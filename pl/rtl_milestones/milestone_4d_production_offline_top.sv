`timescale 1ns/1ps

// Standalone implementation-verification top for 4D-PRODUCTION-OFFLINE.
// The AXI slave is synthesized with an inactive local master stub so the
// implementation can be audited without claiming a Zynq PS Block Design,
// XSA, system base address, or hardware-executable platform integration.
module milestone_4d_production_offline_top (
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
    localparam integer STATUS_WIDTH = 1088;
    localparam integer KEY_DEBOUNCE_CYCLES = 1_000_000;

    wire pl_clk;
    reg [3:0] reset_pipe = 4'b0000;
    wire ctrl_rst_n = reset_pipe[3];
    reg [1:0] key_sync_ff;
    reg [31:0] key_debounce_counter;
    reg key_armed;
    reg key_press_event;

    wire [3:0] raw_inputs = {adc2_dout, adc1_dout, adc2_drdy_n, adc1_drdy_n};
    wire [4:0] state;
    wire [31:0] counter;
    wire busy, done, shutdown_safe, fault_safe;
    wire [1:0] accepted_event_count;
    wire [3:0] synchronized_inputs;
    wire [6:0] control_outputs;
    wire adc_din_i, adc_pwdn_n_i, adc_reset_n_i, adc_sclk_i, adc_start_i;
    wire adc1_cs_n_i, adc2_cs_n_i;
    wire spi_busy, spi_done, spi_start;
    wire [2:0] spi_operation;
    wire [8:0] spi_transfer_bits;
    wire [7:0] ads1_id_raw, ads2_id_raw;
    wire [1:0] id_valid;
    wire [23:0] ads1_id_reads, ads2_id_reads;
    wire [7:0] id_diagnostic_flags;
    wire [3:0] id_summary;
    wire id_gate_passed;
    wire [87:0] ads1_config_readbacks, ads2_config_readbacks;
    wire [10:0] ads1_config_pass_bitmap, ads2_config_pass_bitmap;
    wire config_passed;
    wire [3:0] config_status;
    wire [7:0] current_config_address;
    wire [3:0] current_config_index;
    wire current_config_device;
    wire data_window_active, data_window_passed;
    wire [7:0] data_window_summary;
    wire frame_rx_busy, frame_boundary_safe;
    wire [1:0] drdy_pair_state;
    wire [8:0] frame_received_bits;
    wire [31:0] total_drdy_event_count, complete_pair_count;
    wire [31:0] discarded_settling_count, partial_frame_count;
    wire [31:0] drdy_error_count, frame_timeout_count, spi_error_count;
    wire [31:0] frame_length_error_count, status_header_error_count;
    wire [23:0] last_ads1_status, last_ads2_status;
    wire [191:0] last_ads1_channels, last_ads2_channels;
    wire [15:0] valid_channel_positions, full_error_flags;
    wire [7:0] error_flags, transport_error_flags;
    wire [7:0] register_error_flags, test_error_flags, shutdown_reason;
    wire shutdown_req_seen;
    wire [31:0] watchdog_counter;

    wire acq_axi_arm_pulse, acq_axi_shutdown_pulse, acq_axi_clear_pulse;
    wire arm_request_toggle, shutdown_request_toggle;
    wire clear_request_toggle, snapshot_request_toggle;
    wire arm_ack_toggle, shutdown_ack_toggle;
    wire clear_ack_toggle, snapshot_ack_toggle;
    reg [STATUS_WIDTH-1:0] acq_status_live;
    wire [STATUS_WIDTH-1:0] axi_status_snapshot;

    wire [2:0] id_read_index =
        ((state == 5'd7) || (state == 5'd11)) ? 3'd1 :
        ((state == 5'd8) || (state == 5'd12)) ? 3'd2 :
        ((state == 5'd9) || (state == 5'd13)) ? 3'd3 : 3'd0;
    wire [2:0] synchronized_axi_requests =
        {acq_axi_clear_pulse, acq_axi_shutdown_pulse, acq_axi_arm_pulse};
    wire arm_start_request = key_press_event | acq_axi_arm_pulse;

    IBUFDS #(.DIFF_TERM("FALSE"), .IBUF_LOW_PWR("TRUE")) sys_clk_ibufds (
        .I(sys_clk_p), .IB(sys_clk_n), .O(pl_clk));
    always @(posedge pl_clk) reset_pipe <= {reset_pipe[2:0], 1'b1};

    always @(posedge pl_clk or negedge ctrl_rst_n) begin
        if (!ctrl_rst_n) begin
            key_sync_ff <= 2'b00;
            key_debounce_counter <= 32'd0;
            key_armed <= 1'b0;
            key_press_event <= 1'b0;
        end else begin
            key_sync_ff <= {key_sync_ff[0], pl_key_n};
            key_press_event <= 1'b0;
            if (state != 5'd0) begin
                key_debounce_counter <= 32'd0;
                key_armed <= 1'b0;
            end else if (!key_armed) begin
                if (key_sync_ff[1]) begin
                    if (key_debounce_counter >= KEY_DEBOUNCE_CYCLES - 1) begin
                        key_debounce_counter <= 32'd0;
                        key_armed <= 1'b1;
                    end else key_debounce_counter <= key_debounce_counter + 32'd1;
                end else key_debounce_counter <= 32'd0;
            end else if (!key_sync_ff[1]) begin
                if (key_debounce_counter >= KEY_DEBOUNCE_CYCLES - 1) begin
                    key_debounce_counter <= 32'd0;
                    key_armed <= 1'b0;
                    key_press_event <= 1'b1;
                end else key_debounce_counter <= key_debounce_counter + 32'd1;
            end else key_debounce_counter <= 32'd0;
        end
    end

    (* DONT_TOUCH = "TRUE" *) milestone_4d_production_offline_ctrl ctrl (
        .clk(pl_clk), .rst_n(ctrl_rst_n), .arm_start_req(arm_start_request),
        .software_shutdown_req(acq_axi_shutdown_pulse),
        .clear_sticky_req(acq_axi_clear_pulse), .protocol_fault_in(1'b0),
        .illegal_cs_fault_in(1'b0), .test_data_window_error_in(1'b0),
        .adc1_dout(adc1_dout), .adc2_dout(adc2_dout),
        .adc1_drdy_n(adc1_drdy_n), .adc2_drdy_n(adc2_drdy_n),
        .state(state), .counter(counter), .busy(busy), .done(done),
        .shutdown_safe(shutdown_safe), .fault_safe(fault_safe),
        .accepted_event_count(accepted_event_count),
        .synchronized_inputs(synchronized_inputs), .control_outputs(control_outputs),
        .adc_din(adc_din_i), .adc_pwdn_n(adc_pwdn_n_i),
        .adc_reset_n(adc_reset_n_i), .adc_sclk(adc_sclk_i),
        .adc_start(adc_start_i), .adc1_cs_n(adc1_cs_n_i),
        .adc2_cs_n(adc2_cs_n_i), .spi_busy(spi_busy), .spi_done(spi_done),
        .spi_start(spi_start), .spi_operation(spi_operation),
        .spi_transfer_bits(spi_transfer_bits), .ads1_id_raw(ads1_id_raw),
        .ads2_id_raw(ads2_id_raw), .id_valid(id_valid),
        .ads1_id_reads(ads1_id_reads), .ads2_id_reads(ads2_id_reads),
        .id_diagnostic_flags(id_diagnostic_flags), .id_summary(id_summary),
        .id_gate_passed(id_gate_passed),
        .ads1_config_readbacks(ads1_config_readbacks),
        .ads2_config_readbacks(ads2_config_readbacks),
        .ads1_config_pass_bitmap(ads1_config_pass_bitmap),
        .ads2_config_pass_bitmap(ads2_config_pass_bitmap),
        .config_passed(config_passed), .config_status(config_status),
        .current_config_address(current_config_address),
        .current_config_index(current_config_index),
        .current_config_device(current_config_device),
        .data_window_active(data_window_active),
        .data_window_passed(data_window_passed),
        .data_window_summary(data_window_summary), .frame_rx_busy(frame_rx_busy),
        .frame_boundary_safe(frame_boundary_safe), .drdy_pair_state(drdy_pair_state),
        .frame_received_bits(frame_received_bits),
        .total_drdy_event_count(total_drdy_event_count),
        .complete_pair_count(complete_pair_count),
        .discarded_settling_count(discarded_settling_count),
        .partial_frame_count(partial_frame_count),
        .drdy_error_count(drdy_error_count), .frame_timeout_count(frame_timeout_count),
        .spi_error_count(spi_error_count),
        .frame_length_error_count(frame_length_error_count),
        .status_header_error_count(status_header_error_count),
        .last_ads1_status(last_ads1_status), .last_ads2_status(last_ads2_status),
        .last_ads1_channels(last_ads1_channels),
        .last_ads2_channels(last_ads2_channels),
        .valid_channel_positions(valid_channel_positions),
        .full_error_flags(full_error_flags), .error_flags(error_flags),
        .transport_error_flags(transport_error_flags),
        .register_error_flags(register_error_flags),
        .test_error_flags(test_error_flags), .shutdown_reason(shutdown_reason),
        .shutdown_req_seen(shutdown_req_seen), .watchdog_counter(watchdog_counter));

    // Coherent status bus for the later PS integration.  The unused upper
    // bits are held at zero so future extensions remain backwards compatible.
    always @* begin
        acq_status_live = {STATUS_WIDTH{1'b0}};
        acq_status_live[4:0] = state;
        acq_status_live[5] = busy;
        acq_status_live[6] = id_gate_passed;
        acq_status_live[7] = config_passed;
        acq_status_live[8] = data_window_active;
        acq_status_live[9] = shutdown_safe;
        acq_status_live[10] = fault_safe;
        acq_status_live[26:11] = full_error_flags;
        acq_status_live[50:27] = ads1_id_reads;
        acq_status_live[74:51] = ads2_id_reads;
        acq_status_live[85:75] = ads1_config_pass_bitmap;
        acq_status_live[96:86] = ads2_config_pass_bitmap;
        acq_status_live[128:97] = complete_pair_count;
        acq_status_live[160:129] = discarded_settling_count;
        acq_status_live[192:161] = partial_frame_count;
        acq_status_live[224:193] = drdy_error_count;
        acq_status_live[256:225] = spi_error_count;
        acq_status_live[280:257] = last_ads1_status;
        acq_status_live[304:281] = last_ads2_status;
        acq_status_live[312:305] = data_window_summary;
        acq_status_live[344:313] = watchdog_counter;
        acq_status_live[352:345] = shutdown_reason;
        acq_status_live[440:353] = ads1_config_readbacks;
        acq_status_live[528:441] = ads2_config_readbacks;
        acq_status_live[560:529] = total_drdy_event_count;
        acq_status_live[592:561] = frame_timeout_count;
        acq_status_live[624:593] = frame_length_error_count;
        acq_status_live[656:625] = status_header_error_count;
        acq_status_live[672:657] = valid_channel_positions;
        acq_status_live[676:673] = config_status;
        acq_status_live[677] = frame_boundary_safe;
        acq_status_live[679:678] = accepted_event_count;
        acq_status_live[680] = shutdown_req_seen;
        acq_status_live[872:681] = last_ads1_channels;
        acq_status_live[1064:873] = last_ads2_channels;
    end

    (* DONT_TOUCH = "TRUE" *) milestone_4d_axi_cdc #(
        .STATUS_WIDTH(STATUS_WIDTH)
    ) axi_cdc (
        .axi_clk(pl_clk), .axi_rst_n(ctrl_rst_n), .acq_clk(pl_clk),
        .acq_rst_n(ctrl_rst_n), .arm_request_toggle(arm_request_toggle),
        .shutdown_request_toggle(shutdown_request_toggle),
        .clear_request_toggle(clear_request_toggle),
        .snapshot_request_toggle(snapshot_request_toggle),
        .acq_status_live(acq_status_live), .acq_arm_pulse(acq_axi_arm_pulse),
        .acq_shutdown_pulse(acq_axi_shutdown_pulse),
        .acq_clear_pulse(acq_axi_clear_pulse), .arm_ack_toggle(arm_ack_toggle),
        .shutdown_ack_toggle(shutdown_ack_toggle),
        .clear_ack_toggle(clear_ack_toggle), .snapshot_ack_toggle(snapshot_ack_toggle),
        .axi_status_snapshot(axi_status_snapshot));

    // This local master is intentionally inactive.  It proves the slave in
    // synthesis without presenting the candidate as a PS-connected design.
    (* DONT_TOUCH = "TRUE" *) milestone_4d_axi_lite_regs #(
        .STATUS_WIDTH(STATUS_WIDTH)
    ) axi_regs (
        .s_axi_aclk(pl_clk), .s_axi_aresetn(ctrl_rst_n),
        .s_axi_awaddr(8'd0), .s_axi_awvalid(1'b0), .s_axi_awready(),
        .s_axi_wdata(32'd0), .s_axi_wstrb(4'd0), .s_axi_wvalid(1'b0),
        .s_axi_wready(), .s_axi_bresp(), .s_axi_bvalid(), .s_axi_bready(1'b1),
        .s_axi_araddr(8'd0), .s_axi_arvalid(1'b0), .s_axi_arready(),
        .s_axi_rdata(), .s_axi_rresp(), .s_axi_rvalid(), .s_axi_rready(1'b1),
        .status_snapshot(axi_status_snapshot), .arm_ack_toggle(arm_ack_toggle),
        .shutdown_ack_toggle(shutdown_ack_toggle), .clear_ack_toggle(clear_ack_toggle),
        .snapshot_ack_toggle(snapshot_ack_toggle),
        .arm_request_toggle(arm_request_toggle),
        .shutdown_request_toggle(shutdown_request_toggle),
        .clear_request_toggle(clear_request_toggle),
        .snapshot_request_toggle(snapshot_request_toggle));

    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_din(.I(adc_din_i), .O(adc_din));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_pwdn_n(.I(adc_pwdn_n_i), .O(adc_pwdn_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_reset_n(.I(adc_reset_n_i), .O(adc_reset_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_sclk(.I(adc_sclk_i), .O(adc_sclk));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_start(.I(adc_start_i), .O(adc_start));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc1_cs_n(.I(adc1_cs_n_i), .O(adc1_cs_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc2_cs_n(.I(adc2_cs_n_i), .O(adc2_cs_n));

    // probe0..probe31 retain the preparation build's widths and meanings.
    (* KEEP = "TRUE" *) wire [4:0] ila_probe00_state = state;
    (* KEEP = "TRUE" *) wire [31:0] ila_probe01_counter = counter;
    (* KEEP = "TRUE" *) wire [6:0] ila_probe02_control_outputs = control_outputs;
    (* KEEP = "TRUE" *) wire [1:0] ila_probe03_pwdn_reset = {adc_pwdn_n_i, adc_reset_n_i};
    (* KEEP = "TRUE" *) wire [7:0] ila_probe04_error_flags = error_flags;
    (* KEEP = "TRUE" *) wire ila_probe05_done_test_window = done;
    (* KEEP = "TRUE" *) wire ila_probe06_shutdown_safe = shutdown_safe;
    (* KEEP = "TRUE" *) wire ila_probe07_fault_safe = fault_safe;
    (* KEEP = "TRUE" *) wire [3:0] ila_probe08_synchronized_inputs = synchronized_inputs;
    (* KEEP = "TRUE" *) wire [3:0] ila_probe09_raw_inputs = raw_inputs;
    (* KEEP = "TRUE" *) wire ila_probe10_pl_key_n = pl_key_n;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe11_key_status = {key_sync_ff[1],key_armed,key_press_event};
    (* KEEP = "TRUE" *) wire [1:0] ila_probe12_accepted_event_count = accepted_event_count;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe13_spi_operation = spi_operation;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe14_spi_handshake = {spi_busy,spi_done,spi_start};
    (* KEEP = "TRUE" *) wire [1:0] ila_probe15_chip_selects = {adc1_cs_n_i,adc2_cs_n_i};
    (* KEEP = "TRUE" *) wire [7:0] ila_probe16_ads1_id_raw = ads1_id_raw;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe17_ads2_id_raw = ads2_id_raw;
    (* KEEP = "TRUE" *) wire [1:0] ila_probe18_id_valid = id_valid;
    (* KEEP = "TRUE" *) wire [8:0] ila_probe19_spi_transfer_bits = spi_transfer_bits;
    (* KEEP = "TRUE" *) wire [23:0] ila_probe20_ads1_id_reads = ads1_id_reads;
    (* KEEP = "TRUE" *) wire [23:0] ila_probe21_ads2_id_reads = ads2_id_reads;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe22_id_read_index = id_read_index;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe23_id_diagnostic_flags = id_diagnostic_flags;
    (* KEEP = "TRUE" *) wire [3:0] ila_probe24_id_summary = id_summary;
    (* KEEP = "TRUE" *) wire [3:0] ila_probe25_config_status = config_status;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe26_test_window_status =
        {|full_error_flags[9:5],shutdown_req_seen,data_window_active};
    (* KEEP = "TRUE" *) wire [15:0] ila_probe27_config_readback =
        {ads2_config_readbacks[15:8],ads1_config_readbacks[15:8]};
    (* KEEP = "TRUE" *) wire ila_probe28_software_shutdown_req = acq_axi_shutdown_pulse;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe29_transport_error_flags = transport_error_flags;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe30_register_error_flags = register_error_flags;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe31_test_error_flags = test_error_flags;
    (* KEEP = "TRUE" *) wire [2:0] ila_probe32_axi_requests_synchronized = synchronized_axi_requests;
    (* KEEP = "TRUE" *) wire ila_probe33_shutdown_request_sticky = shutdown_req_seen;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe34_config_address = current_config_address;
    (* KEEP = "TRUE" *) wire [4:0] ila_probe35_config_device_index = {current_config_device,current_config_index};
    (* KEEP = "TRUE" *) wire [10:0] ila_probe36_ads1_config_pass_bitmap = ads1_config_pass_bitmap;
    (* KEEP = "TRUE" *) wire [10:0] ila_probe37_ads2_config_pass_bitmap = ads2_config_pass_bitmap;
    (* KEEP = "TRUE" *) wire [87:0] ila_probe38_ads1_config_readbacks = ads1_config_readbacks;
    (* KEEP = "TRUE" *) wire [87:0] ila_probe39_ads2_config_readbacks = ads2_config_readbacks;
    (* KEEP = "TRUE" *) wire [1:0] ila_probe40_drdy_pair_state = drdy_pair_state;
    (* KEEP = "TRUE" *) wire [10:0] ila_probe41_frame_receive_state =
        {frame_rx_busy,frame_boundary_safe,frame_received_bits};
    (* KEEP = "TRUE" *) wire [31:0] ila_probe42_complete_pair_count = complete_pair_count;
    (* KEEP = "TRUE" *) wire [31:0] ila_probe43_discarded_settling_count = discarded_settling_count;
    (* KEEP = "TRUE" *) wire [31:0] ila_probe44_partial_frame_count = partial_frame_count;
    (* KEEP = "TRUE" *) wire [31:0] ila_probe45_drdy_error_count = drdy_error_count;
    (* KEEP = "TRUE" *) wire [47:0] ila_probe46_last_status_words = {last_ads2_status,last_ads1_status};
    (* KEEP = "TRUE" *) wire [7:0] ila_probe47_data_window_summary = data_window_summary;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe48_shutdown_reason = shutdown_reason;
    (* KEEP = "TRUE" *) wire [15:0] ila_probe49_valid_channel_positions = valid_channel_positions;
    (* KEEP = "TRUE" *) wire [31:0] ila_probe50_total_drdy_event_count = total_drdy_event_count;
    (* KEEP = "TRUE" *) wire [31:0] ila_probe51_frame_timeout_count = frame_timeout_count;
    (* KEEP = "TRUE" *) wire [15:0] ila_probe52_full_error_flags = full_error_flags;
    (* KEEP = "TRUE" *) wire [7:0] ila_probe53_axi_cdc_toggles = {
        snapshot_ack_toggle,snapshot_request_toggle,clear_ack_toggle,clear_request_toggle,
        shutdown_ack_toggle,shutdown_request_toggle,arm_ack_toggle,arm_request_toggle};

    ila_m4d_production_offline ila_i(.clk(pl_clk),
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
        .probe31(ila_probe31_test_error_flags),.probe32(ila_probe32_axi_requests_synchronized),
        .probe33(ila_probe33_shutdown_request_sticky),.probe34(ila_probe34_config_address),
        .probe35(ila_probe35_config_device_index),.probe36(ila_probe36_ads1_config_pass_bitmap),
        .probe37(ila_probe37_ads2_config_pass_bitmap),.probe38(ila_probe38_ads1_config_readbacks),
        .probe39(ila_probe39_ads2_config_readbacks),.probe40(ila_probe40_drdy_pair_state),
        .probe41(ila_probe41_frame_receive_state),.probe42(ila_probe42_complete_pair_count),
        .probe43(ila_probe43_discarded_settling_count),.probe44(ila_probe44_partial_frame_count),
        .probe45(ila_probe45_drdy_error_count),.probe46(ila_probe46_last_status_words),
        .probe47(ila_probe47_data_window_summary),.probe48(ila_probe48_shutdown_reason),
        .probe49(ila_probe49_valid_channel_positions),.probe50(ila_probe50_total_drdy_event_count),
        .probe51(ila_probe51_frame_timeout_count),.probe52(ila_probe52_full_error_flags),
        .probe53(ila_probe53_axi_cdc_toggles));
endmodule
