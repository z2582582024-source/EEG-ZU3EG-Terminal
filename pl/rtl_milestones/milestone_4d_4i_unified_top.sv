`timescale 1ns/1ps
module milestone_4d_4i_unified_top #(
    parameter logic [31:0] TPOR_WAIT_CYCLES=3, VCAP_SETTLE_WAIT_CYCLES=3,
    parameter logic [31:0] RESET_LOW_CYCLES=3, RESET_RELEASE_WAIT_CYCLES=3,
    parameter logic [31:0] SPI_PREPARE_CYCLES=3, SPI_TIMEOUT_CYCLES=1000,
    parameter logic [31:0] START_ASSERT_CYCLES=3, PARTIAL_TIMEOUT_CYCLES=8,
    parameter logic [31:0] DATA_FRAME_TIMEOUT_CYCLES=2000,
    parameter logic [31:0] WATCHDOG_CYCLES=200000,
    parameter integer SYS_CLK_HZ=100, ADS_CLK_HZ=10, SCLK_HALF_DIV=1,
    parameter integer FIFO_DEPTH=4
) (
    input wire acq_clk,input wire acq_rst_n,input wire axi_clk,input wire axi_rst_n,
    input wire adc1_dout,input wire adc2_dout,input wire adc1_drdy_n,input wire adc2_drdy_n,
    input wire protocol_fault_in,input wire illegal_cs_fault_in,input wire test_data_window_error_in,
    output wire adc_din,output wire adc_pwdn_n,output wire adc_reset_n,
    output wire adc_sclk,output wire adc_start,output wire adc1_cs_n,output wire adc2_cs_n,
    input wire [8:0] s_axi_awaddr,input wire s_axi_awvalid,output wire s_axi_awready,
    input wire [31:0] s_axi_wdata,input wire [3:0] s_axi_wstrb,input wire s_axi_wvalid,
    output wire s_axi_wready,output wire [1:0] s_axi_bresp,output wire s_axi_bvalid,input wire s_axi_bready,
    input wire [8:0] s_axi_araddr,input wire s_axi_arvalid,output wire s_axi_arready,
    output wire [31:0] s_axi_rdata,output wire [1:0] s_axi_rresp,output wire s_axi_rvalid,input wire s_axi_rready,
    output wire id_gate_passed,output wire unified_shutdown_safe,output wire unified_fault_safe,
    output wire [31:0] fifo_occupancy,output wire [4:0] controller_state
);
    import milestone_4d_4i_register_pkg::*;
    wire acq_arm_pulse,acq_shutdown_pulse,acq_clear_pulse,acq_status_snapshot_pulse;
    wire acq_fifo_snapshot_pulse,acq_consume_pulse,acq_fault_pulse;
    wire arm_req_t,shutdown_req_t,clear_req_t,status_snapshot_req_t;
    wire fifo_snapshot_req_t,consume_req_t,fault_req_t;
    wire arm_ack_t,shutdown_ack_t,clear_ack_t,status_snapshot_ack_t;
    wire fifo_snapshot_ack_t,consume_ack_t,fault_ack_t;
    wire ctrl_busy,ctrl_done,ctrl_shutdown_safe,ctrl_fault_safe,config_passed;
    wire data_window_active,data_window_passed,frame_boundary_safe;
    wire [15:0] full_error_flags,valid_channel_positions;
    wire [23:0] ads1_id_reads,ads2_id_reads,last_ads1_status,last_ads2_status;
    wire [87:0] ads1_config_readbacks,ads2_config_readbacks;
    wire [10:0] ads1_config_pass_bitmap,ads2_config_pass_bitmap;
    wire [31:0] complete_pair_count,discarded_settling_count,partial_frame_count;
    wire [31:0] drdy_error_count,spi_error_count,total_drdy_event_count;
    wire [31:0] frame_timeout_count,frame_length_error_count,status_header_error_count;
    wire [31:0] watchdog_counter;wire [191:0] last_ads1_channels,last_ads2_channels;
    wire [7:0] data_window_summary,shutdown_reason;wire [3:0] config_status;
    wire [1:0] accepted_event_count;wire shutdown_req_seen;
    wire production_pair_valid,production_partial_event;wire [1:0] production_partial_mask;
    wire [31:0] production_pair_sequence;wire [215:0] production_ads1_frame,production_ads2_frame;
    milestone_4d_production_offline_ctrl #(
        .TPOR_WAIT_CYCLES(TPOR_WAIT_CYCLES),.VCAP_SETTLE_WAIT_CYCLES(VCAP_SETTLE_WAIT_CYCLES),
        .RESET_LOW_CYCLES(RESET_LOW_CYCLES),.RESET_RELEASE_WAIT_CYCLES(RESET_RELEASE_WAIT_CYCLES),
        .SPI_PREPARE_CYCLES(SPI_PREPARE_CYCLES),.SPI_TIMEOUT_CYCLES(SPI_TIMEOUT_CYCLES),
        .START_ASSERT_CYCLES(START_ASSERT_CYCLES),.PARTIAL_TIMEOUT_CYCLES(PARTIAL_TIMEOUT_CYCLES),
        .DATA_FRAME_TIMEOUT_CYCLES(DATA_FRAME_TIMEOUT_CYCLES),.WATCHDOG_CYCLES(WATCHDOG_CYCLES),
        .WINDOW_VALID_FRAMES(8),.SYS_CLK_HZ(SYS_CLK_HZ),.ADS_CLK_HZ(ADS_CLK_HZ),.SCLK_HALF_DIV(SCLK_HALF_DIV)
    ) controller(
        .clk(acq_clk),.rst_n(acq_rst_n),.arm_start_req(acq_arm_pulse),
        .software_shutdown_req(acq_shutdown_pulse),.clear_sticky_req(acq_clear_pulse),
        .protocol_fault_in(protocol_fault_in),.illegal_cs_fault_in(illegal_cs_fault_in),
        .test_data_window_error_in(test_data_window_error_in),.adc1_dout(adc1_dout),.adc2_dout(adc2_dout),
        .adc1_drdy_n(adc1_drdy_n),.adc2_drdy_n(adc2_drdy_n),.state(controller_state),
        .busy(ctrl_busy),.done(ctrl_done),.shutdown_safe(ctrl_shutdown_safe),.fault_safe(ctrl_fault_safe),
        .accepted_event_count(accepted_event_count),.adc_din(adc_din),.adc_pwdn_n(adc_pwdn_n),
        .adc_reset_n(adc_reset_n),.adc_sclk(adc_sclk),.adc_start(adc_start),
        .adc1_cs_n(adc1_cs_n),.adc2_cs_n(adc2_cs_n),.ads1_id_reads(ads1_id_reads),
        .ads2_id_reads(ads2_id_reads),.id_gate_passed(id_gate_passed),
        .ads1_config_readbacks(ads1_config_readbacks),.ads2_config_readbacks(ads2_config_readbacks),
        .ads1_config_pass_bitmap(ads1_config_pass_bitmap),.ads2_config_pass_bitmap(ads2_config_pass_bitmap),
        .config_passed(config_passed),.config_status(config_status),.data_window_active(data_window_active),
        .data_window_passed(data_window_passed),.data_window_summary(data_window_summary),
        .frame_boundary_safe(frame_boundary_safe),.total_drdy_event_count(total_drdy_event_count),
        .complete_pair_count(complete_pair_count),.discarded_settling_count(discarded_settling_count),
        .partial_frame_count(partial_frame_count),.drdy_error_count(drdy_error_count),
        .frame_timeout_count(frame_timeout_count),.spi_error_count(spi_error_count),
        .frame_length_error_count(frame_length_error_count),.status_header_error_count(status_header_error_count),
        .last_ads1_status(last_ads1_status),.last_ads2_status(last_ads2_status),
        .last_ads1_channels(last_ads1_channels),.last_ads2_channels(last_ads2_channels),
        .valid_channel_positions(valid_channel_positions),.full_error_flags(full_error_flags),
        .shutdown_reason(shutdown_reason),.shutdown_req_seen(shutdown_req_seen),.watchdog_counter(watchdog_counter),
        .production_pair_valid(production_pair_valid),.production_pair_sequence(production_pair_sequence),
        .production_ads1_frame(production_ads1_frame),.production_ads2_frame(production_ads2_frame),
        .production_partial_event(production_partial_event),.production_partial_mask(production_partial_mask));

    wire fifo_consume_req,fifo_consume_valid,fifo_empty,fifo_full,fifo_almost_full;
    wire fifo_overflow,fifo_partial,fifo_gap,fifo_stopped,fifo_shutdown_safe,fifo_fault_safe;
    wire [31:0] fifo_overflow_count,fifo_dropped_count,fifo_partial_count,fifo_gap_count;
    wire [31:0] fifo_last_accepted,fifo_last_consumed,head_sequence,consume_sequence;
    wire head_valid;wire [215:0] head_ads1_frame,head_ads2_frame,consume_ads1_frame,consume_ads2_frame;
    wire [23:0] head_ads1_status,head_ads2_status,consume_ads1_status,consume_ads2_status;
    wire fifo_shutdown_req=acq_shutdown_pulse|ctrl_done|ctrl_shutdown_safe;
    wire fifo_fault_req=acq_fault_pulse|ctrl_fault_safe;
    milestone_4f_frame_pair_fifo #(.DEPTH(FIFO_DEPTH)) frame_fifo(
        .clk(acq_clk),.rst_n(acq_rst_n),.id_gate_passed(id_gate_passed),
        .frame1_valid(production_pair_valid|production_partial_event),.frame2_valid(production_pair_valid),
        .producer_sequence(production_pair_sequence),.ads1_frame_in(production_ads1_frame),
        .ads2_frame_in(production_ads2_frame),.ads1_status_in(production_ads1_frame[215:192]),
        .ads2_status_in(production_ads2_frame[215:192]),.consume_req(fifo_consume_req),
        .shutdown_req(fifo_shutdown_req),.fault_req(fifo_fault_req),.consume_valid(fifo_consume_valid),
        .consume_sequence(consume_sequence),.consume_ads1_frame(consume_ads1_frame),
        .consume_ads2_frame(consume_ads2_frame),.consume_ads1_status(consume_ads1_status),
        .consume_ads2_status(consume_ads2_status),.empty(fifo_empty),.full(fifo_full),
        .almost_full(fifo_almost_full),.occupancy(fifo_occupancy),.overflow_count(fifo_overflow_count),
        .dropped_count(fifo_dropped_count),.partial_pair_count(fifo_partial_count),
        .sequence_gap_count(fifo_gap_count),.last_accepted_sequence(fifo_last_accepted),
        .last_consumed_sequence(fifo_last_consumed),.sequence_gap(fifo_gap),.partial_pair(fifo_partial),
        .overflow(fifo_overflow),.stopped(fifo_stopped),.shutdown_safe(fifo_shutdown_safe),
        .fault_safe(fifo_fault_safe),.head_valid(head_valid),.head_sequence(head_sequence),
        .head_ads1_frame(head_ads1_frame),.head_ads2_frame(head_ads2_frame),
        .head_ads1_status(head_ads1_status),.head_ads2_status(head_ads2_status));

    wire snapshot_valid,token_error_sticky;wire [31:0] snapshot_token,snapshot_sequence,token_error_count;
    wire [215:0] snapshot_ads1_frame,snapshot_ads2_frame;wire [23:0] snapshot_ads1_status,snapshot_ads2_status;
    milestone_4d_4i_fifo_snapshot snapshot_bridge(
        .clk(acq_clk),.rst_n(acq_rst_n),.snapshot_req(acq_fifo_snapshot_pulse),
        .consume_ack_req(acq_consume_pulse),.fault_req(fifo_fault_req),.head_valid(head_valid),
        .head_sequence(head_sequence),.head_ads1_frame(head_ads1_frame),.head_ads2_frame(head_ads2_frame),
        .head_ads1_status(head_ads1_status),.head_ads2_status(head_ads2_status),
        .fifo_consume_req(fifo_consume_req),.snapshot_valid(snapshot_valid),.snapshot_token(snapshot_token),
        .snapshot_sequence(snapshot_sequence),.snapshot_ads1_frame(snapshot_ads1_frame),
        .snapshot_ads2_frame(snapshot_ads2_frame),.snapshot_ads1_status(snapshot_ads1_status),
        .snapshot_ads2_status(snapshot_ads2_status),.token_error_count(token_error_count),
        .token_error_sticky(token_error_sticky));
    assign unified_shutdown_safe=ctrl_shutdown_safe&&fifo_shutdown_safe;
    assign unified_fault_safe=ctrl_fault_safe||fifo_fault_safe;

    reg [UNIFIED_STATUS_WIDTH-1:0] acq_status_live;wire [UNIFIED_STATUS_WIDTH-1:0] axi_status_snapshot;
    wire [8:0] fifo_status_bits={token_error_sticky,snapshot_valid,fifo_fault_safe,
        fifo_shutdown_safe,fifo_stopped,fifo_overflow,fifo_almost_full,fifo_full,fifo_empty};
    always @* begin
        acq_status_live={UNIFIED_STATUS_WIDTH{1'b0}};
        acq_status_live[4:0]=controller_state;acq_status_live[5]=ctrl_busy;
        acq_status_live[6]=id_gate_passed;acq_status_live[7]=config_passed;
        acq_status_live[8]=data_window_active;acq_status_live[9]=unified_shutdown_safe;
        acq_status_live[10]=unified_fault_safe;acq_status_live[26:11]=full_error_flags;
        acq_status_live[50:27]=ads1_id_reads;acq_status_live[74:51]=ads2_id_reads;
        acq_status_live[85:75]=ads1_config_pass_bitmap;acq_status_live[96:86]=ads2_config_pass_bitmap;
        acq_status_live[128:97]=complete_pair_count;acq_status_live[160:129]=discarded_settling_count;
        acq_status_live[192:161]=partial_frame_count;acq_status_live[224:193]=drdy_error_count;
        acq_status_live[256:225]=spi_error_count;acq_status_live[280:257]=last_ads1_status;
        acq_status_live[304:281]=last_ads2_status;acq_status_live[312:305]=data_window_summary;
        acq_status_live[344:313]=watchdog_counter;acq_status_live[352:345]=shutdown_reason;
        acq_status_live[440:353]=ads1_config_readbacks;acq_status_live[528:441]=ads2_config_readbacks;
        acq_status_live[560:529]=total_drdy_event_count;acq_status_live[592:561]=frame_timeout_count;
        acq_status_live[624:593]=frame_length_error_count;acq_status_live[656:625]=status_header_error_count;
        acq_status_live[672:657]=valid_channel_positions;acq_status_live[676:673]=config_status;
        acq_status_live[677]=frame_boundary_safe;acq_status_live[679:678]=accepted_event_count;
        acq_status_live[680]=shutdown_req_seen;acq_status_live[872:681]=last_ads1_channels;
        acq_status_live[1064:873]=last_ads2_channels;
        acq_status_live[UF_FIFO_STATUS_LSB+:9]=fifo_status_bits;
        acq_status_live[UF_OCCUPANCY_LSB+:32]=fifo_occupancy;
        acq_status_live[UF_CAPACITY_LSB+:32]=FIFO_DEPTH;
        acq_status_live[UF_PRODUCER_LSB+:32]=production_pair_sequence;
        acq_status_live[UF_CONSUMER_LSB+:32]=fifo_last_consumed;
        acq_status_live[UF_OVERFLOW_LSB+:32]=fifo_overflow_count;
        acq_status_live[UF_DROPPED_LSB+:32]=fifo_dropped_count;
        acq_status_live[UF_PARTIAL_LSB+:32]=fifo_partial_count;
        acq_status_live[UF_GAP_LSB+:32]=fifo_gap_count;
        acq_status_live[UF_LAST_ACCEPTED_LSB+:32]=fifo_last_accepted;
        acq_status_live[UF_LAST_CONSUMED_LSB+:32]=fifo_last_consumed;
        acq_status_live[UF_TOKEN_LSB+:32]=snapshot_token;
        acq_status_live[UF_SNAPSHOT_VALID_BIT]=snapshot_valid;
        acq_status_live[UF_SNAPSHOT_SEQUENCE_LSB+:32]=snapshot_sequence;
        acq_status_live[UF_SNAPSHOT_ADS1_STATUS_LSB+:24]=snapshot_ads1_status;
        acq_status_live[UF_SNAPSHOT_ADS2_STATUS_LSB+:24]=snapshot_ads2_status;
        acq_status_live[UF_SNAPSHOT_ADS1_FRAME_LSB+:216]=snapshot_ads1_frame;
        acq_status_live[UF_SNAPSHOT_ADS2_FRAME_LSB+:216]=snapshot_ads2_frame;
        acq_status_live[UF_TOKEN_ERROR_LSB+:32]=token_error_count;
    end
    milestone_4d_4i_axi_cdc #(.STATUS_WIDTH(UNIFIED_STATUS_WIDTH)) unified_cdc(
        .axi_clk(axi_clk),.axi_rst_n(axi_rst_n),.acq_clk(acq_clk),.acq_rst_n(acq_rst_n),
        .arm_request_toggle(arm_req_t),.shutdown_request_toggle(shutdown_req_t),
        .clear_request_toggle(clear_req_t),.snapshot_request_toggle(status_snapshot_req_t),
        .fifo_snapshot_request_toggle(fifo_snapshot_req_t),.consume_request_toggle(consume_req_t),
        .fault_request_toggle(fault_req_t),.acq_status_live(acq_status_live),
        .acq_arm_pulse(acq_arm_pulse),.acq_shutdown_pulse(acq_shutdown_pulse),
        .acq_clear_pulse(acq_clear_pulse),.arm_ack_toggle(arm_ack_t),
        .shutdown_ack_toggle(shutdown_ack_t),.clear_ack_toggle(clear_ack_t),
        .snapshot_ack_toggle(status_snapshot_ack_t),.axi_status_snapshot(axi_status_snapshot),
        .acq_fifo_snapshot_pulse(acq_fifo_snapshot_pulse),.acq_consume_pulse(acq_consume_pulse),
        .acq_fault_pulse(acq_fault_pulse),.fifo_snapshot_ack_toggle(fifo_snapshot_ack_t),
        .consume_ack_toggle(consume_ack_t),.fault_ack_toggle(fault_ack_t));
    milestone_4d_4i_axi_lite_regs #(.STATUS_WIDTH(UNIFIED_STATUS_WIDTH)) unified_regs(
        .s_axi_aclk(axi_clk),.s_axi_aresetn(axi_rst_n),.s_axi_awaddr(s_axi_awaddr),
        .s_axi_awvalid(s_axi_awvalid),.s_axi_awready(s_axi_awready),.s_axi_wdata(s_axi_wdata),
        .s_axi_wstrb(s_axi_wstrb),.s_axi_wvalid(s_axi_wvalid),.s_axi_wready(s_axi_wready),
        .s_axi_bresp(s_axi_bresp),.s_axi_bvalid(s_axi_bvalid),.s_axi_bready(s_axi_bready),
        .s_axi_araddr(s_axi_araddr),.s_axi_arvalid(s_axi_arvalid),.s_axi_arready(s_axi_arready),
        .s_axi_rdata(s_axi_rdata),.s_axi_rresp(s_axi_rresp),.s_axi_rvalid(s_axi_rvalid),.s_axi_rready(s_axi_rready),
        .status_snapshot(axi_status_snapshot),.arm_ack_toggle(arm_ack_t),
        .shutdown_ack_toggle(shutdown_ack_t),.clear_ack_toggle(clear_ack_t),
        .snapshot_ack_toggle(status_snapshot_ack_t),.fifo_snapshot_ack_toggle(fifo_snapshot_ack_t),
        .consume_ack_toggle(consume_ack_t),.fault_ack_toggle(fault_ack_t),
        .arm_request_toggle(arm_req_t),.shutdown_request_toggle(shutdown_req_t),
        .clear_request_toggle(clear_req_t),.snapshot_request_toggle(status_snapshot_req_t),
        .fifo_snapshot_request_toggle(fifo_snapshot_req_t),.consume_request_toggle(consume_req_t),
        .fault_request_toggle(fault_req_t));
endmodule
