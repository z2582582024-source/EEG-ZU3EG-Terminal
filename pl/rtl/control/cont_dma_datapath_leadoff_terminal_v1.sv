`timescale 1ns/1ps

// Synthesizable explicit-interface candidate joining the independent 4J/4K
// front end to the existing POST_4I FIFO/snapshot/CDC/register back end.
// No design behavior depends on cross-hierarchy references.
module cont_dma_datapath_leadoff_terminal_v1 #(
    parameter integer TPOR_WAIT_CYCLES=21_000_000,
    parameter integer VCAP_SETTLE_WAIT_CYCLES=100_000_000,
    parameter integer RESET_LOW_CYCLES=100_000,
    parameter integer RESET_RELEASE_WAIT_CYCLES=100_000,
    parameter integer SPI_PREPARE_CYCLES=200,
    parameter integer SPI_TIMEOUT_CYCLES=100_000,
    parameter integer EXIT_TIMEOUT_CYCLES=100_000,
    parameter integer START_ASSERT_CYCLES=200,
    parameter integer PARTIAL_TIMEOUT_CYCLES=400_000,
    parameter integer DATA_FRAME_TIMEOUT_CYCLES=2_000_000,
    parameter integer WATCHDOG_CYCLES=0,
    parameter integer WINDOW_VALID_FRAMES=0,
    parameter integer FIFO_DEPTH=256,
    parameter logic [7:0] CONFIG1_BYTE=8'h96,
    parameter [31:0] SAMPLE_INTERVAL_NS=32'd4096000,
    parameter integer STOP_ALIGN_LOG2=5
)(
    output wire [31:0] m_axis_tdata,output wire [3:0] m_axis_tkeep,
    output wire m_axis_tvalid,input wire m_axis_tready,output wire m_axis_tlast,
    output wire [63:0] m_axis_frame_cycle,
    input wire acq_clk,input wire acq_rst_n,input wire axi_clk,input wire axi_rst_n,
    input wire supply_valid,input wire clock_loss_in,input wire exit_comm_fault_req,
    input wire protocol_fault_req,input wire illegal_cs_fault_req,
    input wire adc1_dout,input wire adc2_dout,input wire adc1_drdy_n,input wire adc2_drdy_n,
    output wire adc_clk,output wire adc_din,output wire adc_pwdn_n,output wire adc_reset_n,
    output wire adc_sclk,output wire adc_start,output wire adc1_cs_n,output wire adc2_cs_n,
    input wire [8:0] s_axi_awaddr,input wire s_axi_awvalid,output wire s_axi_awready,
    input wire [31:0] s_axi_wdata,input wire [3:0] s_axi_wstrb,input wire s_axi_wvalid,
    output wire s_axi_wready,output wire [1:0] s_axi_bresp,output wire s_axi_bvalid,input wire s_axi_bready,
    input wire [8:0] s_axi_araddr,input wire s_axi_arvalid,output wire s_axi_arready,
    output wire [31:0] s_axi_rdata,output wire [1:0] s_axi_rresp,output wire s_axi_rvalid,input wire s_axi_rready,
    output wire id_gate_passed,output wire config_passed,output wire unified_shutdown_safe,
    output wire unified_fault_safe,output wire controller_fault_pending,output wire system_fault_latched,
    output wire quarantine_active,output wire controller_fault_safe,output wire fifo_fault_safe,
    output wire controller_shutdown_safe,output wire fifo_shutdown_safe,output wire fifo_stopped,
    output wire fifo_fault_req,output wire fifo_shutdown_req,output wire fifo_consume_req,
    output wire acq_fault_pulse,output wire acq_shutdown_pulse,output wire acq_consume_pulse,
    output wire [31:0] fifo_occupancy,output wire [4:0] controller_state,
    output wire production_pair_valid,output wire [31:0] production_pair_sequence,
    output wire [215:0] production_ads1_frame,output wire [215:0] production_ads2_frame,
    output wire production_partial_event,output wire [1:0] production_partial_mask,
    output wire fifo_write_accept,output wire fifo_read_accept,output wire fifo_consume_valid,
    output wire fifo_empty,output wire fifo_full,output wire fifo_overflow,
    output wire fifo_partial,output wire head_valid,
    output wire [31:0] head_sequence,output wire [215:0] head_ads1_frame,
    output wire [215:0] head_ads2_frame,output wire snapshot_valid,
    output wire [31:0] snapshot_token,output wire [31:0] snapshot_sequence,
    output wire [215:0] snapshot_ads1_frame,output wire [215:0] snapshot_ads2_frame,
    output wire [23:0] ads1_id_reads,output wire [23:0] ads2_id_reads,
    output wire [119:0] ads1_config_readbacks,output wire [119:0] ads2_config_readbacks,
    output wire [14:0] ads1_config_pass_bitmap,output wire [14:0] ads2_config_pass_bitmap,
    output wire [3:0] config_status,output wire frame_boundary_safe,
    output wire [31:0] complete_pair_count,output wire [31:0] discarded_settling_count,
    output wire [31:0] partial_frame_count,output wire [31:0] drdy_error_count,
    output wire [31:0] spi_error_count,output wire [31:0] total_drdy_event_count,
    output wire [31:0] frame_timeout_count,output wire [31:0] frame_length_error_count,
    output wire [31:0] status_header_error_count,output wire [23:0] last_ads1_status,
    output wire [23:0] last_ads2_status,output wire [191:0] last_ads1_channels,
    output wire [191:0] last_ads2_channels,output wire [15:0] valid_channel_positions,
    output wire [7:0] data_window_summary,output wire [31:0] watchdog_counter,
    output wire shutdown_req_seen,output wire [15:0] full_error_flags,
    output wire [7:0] shutdown_reason,output wire [1:0] accepted_event_count,
    output wire data_window_active,output wire data_window_passed,
    output wire clock_loss_latched,output wire controller_command_accept,
    output wire [31:0] transactions_accepted_before_loss,
    output wire [31:0] transactions_accepted_after_loss,
    output wire controller_frame_accept,output wire controller_spi_busy,
    output wire controller_spi_done,output wire controller_spi_start,
    output wire [2:0] controller_spi_operation,output wire [1:0] exit_accept_mask,
    output wire [1:0] exit_phy_done_mask,output wire [1:0] exit_success_mask,
    output wire parameter_contract_ok,
    output wire lead_off_busy,output wire lead_off_done,output wire lead_off_failed,
    output wire lead_off_unknown,output wire [7:0] lead_off_error_code,
    output wire [15:0] lead_off_status_samples_adc1,output wire [15:0] lead_off_status_samples_adc2,
    output wire [7:0] lead_off_positive_off_mask_adc1,output wire [7:0] lead_off_negative_off_mask_adc1,
    output wire [7:0] lead_off_positive_off_mask_adc2,output wire [7:0] lead_off_negative_off_mask_adc2,
    output wire lead_off_restore_verified_adc1,output wire lead_off_restore_verified_adc2,
    output wire lead_off_continuous_resumed,output wire [4:0] lead_off_phase,
    output wire [4:0] lead_off_register_index,output wire lead_off_error_adc,
    output wire [7:0] lead_off_error_register_address,output wire [7:0] lead_off_error_expected,
    output wire [7:0] lead_off_error_actual
);
    import milestone_4d_4i_register_pkg::*;
    localparam integer EXTERNAL_STATUS_WIDTH=2152;
    localparam integer FRONT_SYS_CLK_HZ=100_000_000;
    localparam integer FRONT_ADS_CLK_HZ=2_000_000;
    localparam integer FRONT_HALF_PERIOD_CYCLES=25;
    localparam integer FRONT_SCLK_HALF_DIV=50;
    wire acq_arm_pulse,acq_clear_pulse,acq_status_snapshot_pulse;
    reg chunk_stop_pending=0,chunk_shutdown_pulse=0;
    reg heartbeat_seen=0,lease_expired=0;
    wire acq_fifo_snapshot_pulse;
    wire arm_req_t,shutdown_req_t,clear_req_t,status_snapshot_req_t;
    wire fifo_snapshot_req_t,consume_req_t,fault_req_t;
    wire arm_ack_t,shutdown_ack_t,clear_ack_t,status_snapshot_ack_t;
    wire fifo_snapshot_ack_t,consume_ack_t,fault_ack_t;
    wire clock_loss_fault_safe,clock_loss_inflight_at_detection;
    wire [31:0] clock_loss_response_cycles;
    wire [7:0] id_diagnostic_flags; wire [3:0] id_summary;
    wire [31:0] lead_off_status_word,lead_off_error_word,lead_off_sample_counts,
        lead_off_off_masks,lead_off_owner_state,lead_off_phase_word;
    wire lead_off_start_request,lead_off_cancel_request,lead_off_acknowledge_request;
    wire initialize_request;
    wire [EXTERNAL_STATUS_WIDTH-1:0] axi_status_snapshot;
    reg [63:0] cycle_counter=0,frame_accept_cycle=0;
    reg frame_cycle_pending=0,frame_metadata_fault=0;
    reg [63:0] frame_cycles[0:255];
    reg [31:0] frame_cycle_sequence[0:255];
    reg [255:0] frame_cycle_valid=0;
    wire head_metadata_valid=frame_cycle_valid[head_sequence[7:0]] &&
        frame_cycle_sequence[head_sequence[7:0]]==head_sequence;
    always @(posedge acq_clk or negedge acq_rst_n)begin
        if(!acq_rst_n)begin
            cycle_counter<=0;frame_accept_cycle<=0;frame_cycle_pending<=0;
            frame_metadata_fault<=0;frame_cycle_valid<=0;
        end else begin
            cycle_counter<=cycle_counter+1'b1;
            if(controller_frame_accept)begin frame_accept_cycle<=cycle_counter;frame_cycle_pending<=1;end
            if(production_pair_valid)begin
                if(!frame_cycle_pending || (cycle_counter-frame_accept_cycle)>64'd21904)
                    frame_metadata_fault<=1;
                frame_cycle_pending<=0;
            end
            if(fifo_write_accept)begin
                frame_cycle_valid[production_pair_sequence[7:0]]<=1;
            end
            if(head_valid && !head_metadata_valid)frame_metadata_fault<=1;
        end
    end
    always @(posedge acq_clk)begin
        if(acq_rst_n && fifo_write_accept)begin
            frame_cycles[production_pair_sequence[7:0]]<=frame_accept_cycle;
            frame_cycle_sequence[production_pair_sequence[7:0]]<=production_pair_sequence;
        end
    end
    wire [31:0] initialize_status={25'd0,axi_status_snapshot[1065],axi_status_snapshot[7],
        axi_status_snapshot[6],axi_status_snapshot[10],axi_status_snapshot[9],
        ((axi_status_snapshot[4:0]!=0)&&(axi_status_snapshot[4:0]!=30)),
        (axi_status_snapshot[4:0]==30)};
    wire [31:0] calibration_config,calibration_generation,calibration_sample_count;
    wire [31:0] calibration_bound_config,calibration_result_flags;
    wire [9:0] calibration_read_index;
    wire signed [31:0] calibration_read_sample;
    wire calibration_start_request;
    reg calibration_mode=0;
    always @(posedge acq_clk or negedge acq_rst_n)begin
        if(!acq_rst_n)calibration_mode<=0;
        else if(lead_off_start_request)calibration_mode<=0;
        else if(calibration_start_request)calibration_mode<=1;
    end
    // The original production top explicitly requires the single shared100MHz
    // acquisition/AXI clock and coordinatedreset. No new asynchronous buspath.
    wire [31:0] active_calibration_config=lead_off_start_request?32'd0:
        (calibration_start_request||calibration_mode)?calibration_config:32'd0;
    wire [4:0] lead_off_ctrl_phase,lead_off_ctrl_register_index;
    wire lead_off_ctrl_error_adc;
    wire [7:0] lead_off_ctrl_error_address,lead_off_ctrl_error_expected,lead_off_ctrl_error_actual;
    wire [7:0] lead_off_ctrl_error_code;
    wire [15:0] lead_off_ctrl_samples_adc1,lead_off_ctrl_samples_adc2;
    wire [7:0] lead_off_ctrl_pos_adc1,lead_off_ctrl_neg_adc1,lead_off_ctrl_pos_adc2,lead_off_ctrl_neg_adc2;
    wire lead_off_ctrl_restore_adc1,lead_off_ctrl_restore_adc2,lead_off_ctrl_resumed;
    wire [6:0] control_outputs; wire controller_busy;
    wire [2:0] adc_clk_state; wire adc_clk_shutdown_safe,adc_clk_fault_safe;
    wire adc_clk_start_accepted,adc_clk_running,frame_rx_busy;
    wire exit_comm_fault_latched,exit_timeout_latched;
    wire [31:0] exit_abort_response_cycles;

    cont_front_adapter_leadoff_terminal_v1 #(
        .SYS_CLK_HZ(FRONT_SYS_CLK_HZ),.HALF_PERIOD_CYCLES(FRONT_HALF_PERIOD_CYCLES),
        .CTRL_SYS_CLK_HZ(FRONT_SYS_CLK_HZ),.CTRL_ADS_CLK_HZ(FRONT_ADS_CLK_HZ),
        .ADS_CLK_HZ(FRONT_ADS_CLK_HZ),.SCLK_HALF_DIV(FRONT_SCLK_HALF_DIV),
        .TPOR_WAIT_CYCLES(TPOR_WAIT_CYCLES),.VCAP_SETTLE_WAIT_CYCLES(VCAP_SETTLE_WAIT_CYCLES),
        .RESET_LOW_CYCLES(RESET_LOW_CYCLES),.RESET_RELEASE_WAIT_CYCLES(RESET_RELEASE_WAIT_CYCLES),
        .SPI_PREPARE_CYCLES(SPI_PREPARE_CYCLES),.SPI_TIMEOUT_CYCLES(SPI_TIMEOUT_CYCLES),
        .EXIT_TIMEOUT_CYCLES(EXIT_TIMEOUT_CYCLES),.START_ASSERT_CYCLES(START_ASSERT_CYCLES),
        .PARTIAL_TIMEOUT_CYCLES(PARTIAL_TIMEOUT_CYCLES),.DATA_FRAME_TIMEOUT_CYCLES(DATA_FRAME_TIMEOUT_CYCLES),
        .WATCHDOG_CYCLES(WATCHDOG_CYCLES),.WINDOW_VALID_FRAMES(WINDOW_VALID_FRAMES),.CONFIG1_BYTE(CONFIG1_BYTE)
    ) u_front(
        .calibration_config(active_calibration_config),.calibration_read_index(calibration_read_index),
        .calibration_read_sample(calibration_read_sample),.calibration_generation(calibration_generation),
        .calibration_sample_count(calibration_sample_count),.calibration_bound_config(calibration_bound_config),
        .calibration_result_flags(calibration_result_flags),
        .clk(acq_clk),.rst_n(acq_rst_n),.supply_valid(supply_valid),
        .clock_start_req(acq_arm_pulse|initialize_request),.controller_arm_req(acq_arm_pulse),
        .controller_initialize_req(initialize_request),
        .clock_loss_in(clock_loss_in),.exit_comm_fault_req(exit_comm_fault_req),
        .system_fault_req(acq_fault_pulse|lease_expired|frame_metadata_fault),
        .software_shutdown_req(chunk_shutdown_pulse),.protocol_fault_req(protocol_fault_req),
        .illegal_cs_fault_req(illegal_cs_fault_req),.lead_off_start(lead_off_start_request|calibration_start_request),
        .lead_off_cancel(lead_off_cancel_request),.lead_off_acknowledge(lead_off_acknowledge_request),
        .adc1_dout(adc1_dout),.adc2_dout(adc2_dout),
        .adc1_drdy_n(adc1_drdy_n),.adc2_drdy_n(adc2_drdy_n),.adc_clk(adc_clk),
        .adc_clk_state(adc_clk_state),.adc_clk_shutdown_safe(adc_clk_shutdown_safe),
        .adc_clk_fault_safe(adc_clk_fault_safe),.adc_clk_start_accepted(adc_clk_start_accepted),
        .adc_clk_running(adc_clk_running),.controller_state(controller_state),
        .controller_busy(controller_busy),.controller_shutdown_safe(controller_shutdown_safe),
        .controller_fault_safe(controller_fault_safe),.control_outputs(control_outputs),
        .adc_pwdn_n(adc_pwdn_n),.adc_reset_n(adc_reset_n),.adc_sclk(adc_sclk),
        .adc_din(adc_din),.adc_start(adc_start),.adc1_cs_n(adc1_cs_n),.adc2_cs_n(adc2_cs_n),
        .ads1_id_reads(ads1_id_reads),.ads2_id_reads(ads2_id_reads),
        .id_diagnostic_flags(id_diagnostic_flags),.id_summary(id_summary),
        .id_gate_passed(id_gate_passed),.full_error_flags(full_error_flags),
        .accepted_event_count(accepted_event_count),.data_window_active(data_window_active),
        .data_window_passed(data_window_passed),.clock_loss_latched(clock_loss_latched),
        .clock_loss_fault_safe(clock_loss_fault_safe),
        .clock_loss_inflight_at_detection(clock_loss_inflight_at_detection),
        .clock_loss_response_cycles(clock_loss_response_cycles),
        .transactions_accepted_before_loss(transactions_accepted_before_loss),
        .transactions_accepted_after_loss(transactions_accepted_after_loss),
        .controller_spi_busy(controller_spi_busy),.controller_spi_start(controller_spi_start),
        .controller_spi_operation(controller_spi_operation),.controller_spi_done(controller_spi_done),
        .controller_command_accept(controller_command_accept),
        .controller_frame_accept(controller_frame_accept),
        .controller_fault_pending(controller_fault_pending),.system_fault_latched(system_fault_latched),
        .exit_accept_mask(exit_accept_mask),.exit_phy_done_mask(exit_phy_done_mask),
        .exit_success_mask(exit_success_mask),.exit_comm_fault_latched(exit_comm_fault_latched),
        .exit_timeout_latched(exit_timeout_latched),.exit_abort_response_cycles(exit_abort_response_cycles),
        .config_passed(config_passed),.config_status(config_status),
        .ads1_config_readbacks(ads1_config_readbacks),.ads2_config_readbacks(ads2_config_readbacks),
        .ads1_config_pass_bitmap(ads1_config_pass_bitmap),
        .ads2_config_pass_bitmap(ads2_config_pass_bitmap),
        .frame_boundary_safe(frame_boundary_safe),.frame_rx_busy(frame_rx_busy),
        .complete_pair_count(complete_pair_count),.discarded_settling_count(discarded_settling_count),
        .partial_frame_count(partial_frame_count),.drdy_error_count(drdy_error_count),
        .spi_error_count(spi_error_count),.total_drdy_event_count(total_drdy_event_count),
        .frame_timeout_count(frame_timeout_count),.frame_length_error_count(frame_length_error_count),
        .status_header_error_count(status_header_error_count),.last_ads1_status(last_ads1_status),
        .last_ads2_status(last_ads2_status),.last_ads1_channels(last_ads1_channels),
        .last_ads2_channels(last_ads2_channels),.valid_channel_positions(valid_channel_positions),
        .data_window_summary(data_window_summary),.watchdog_counter(watchdog_counter),
        .shutdown_req_seen(shutdown_req_seen),.production_pair_valid(production_pair_valid),
        .production_pair_sequence(production_pair_sequence),.production_ads1_frame(production_ads1_frame),
        .production_ads2_frame(production_ads2_frame),.production_partial_event(production_partial_event),
        .production_partial_mask(production_partial_mask),.shutdown_reason(shutdown_reason),
        .lead_off_busy(lead_off_busy),.lead_off_done(lead_off_done),.lead_off_failed(lead_off_failed),
        .lead_off_unknown(lead_off_unknown),.lead_off_error_code(lead_off_ctrl_error_code),
        .lead_off_status_samples_adc1(lead_off_ctrl_samples_adc1),
        .lead_off_status_samples_adc2(lead_off_ctrl_samples_adc2),
        .lead_off_positive_off_mask_adc1(lead_off_ctrl_pos_adc1),
        .lead_off_negative_off_mask_adc1(lead_off_ctrl_neg_adc1),
        .lead_off_positive_off_mask_adc2(lead_off_ctrl_pos_adc2),
        .lead_off_negative_off_mask_adc2(lead_off_ctrl_neg_adc2),
        .lead_off_restore_verified_adc1(lead_off_ctrl_restore_adc1),
        .lead_off_restore_verified_adc2(lead_off_ctrl_restore_adc2),
        .lead_off_continuous_resumed(lead_off_ctrl_resumed),.lead_off_phase(lead_off_ctrl_phase),
        .lead_off_register_index(lead_off_ctrl_register_index),.lead_off_error_adc(lead_off_ctrl_error_adc),
        .lead_off_error_register_address(lead_off_ctrl_error_address),
        .lead_off_error_expected(lead_off_ctrl_error_expected),.lead_off_error_actual(lead_off_ctrl_error_actual));
    assign lead_off_error_code=lead_off_ctrl_error_code;
    assign lead_off_status_samples_adc1=lead_off_ctrl_samples_adc1;
    assign lead_off_status_samples_adc2=lead_off_ctrl_samples_adc2;
    assign lead_off_positive_off_mask_adc1=lead_off_ctrl_pos_adc1;
    assign lead_off_negative_off_mask_adc1=lead_off_ctrl_neg_adc1;
    assign lead_off_positive_off_mask_adc2=lead_off_ctrl_pos_adc2;
    assign lead_off_negative_off_mask_adc2=lead_off_ctrl_neg_adc2;
    assign lead_off_restore_verified_adc1=lead_off_ctrl_restore_adc1;
    assign lead_off_restore_verified_adc2=lead_off_ctrl_restore_adc2;
    assign lead_off_continuous_resumed=lead_off_ctrl_resumed;assign lead_off_phase=lead_off_ctrl_phase;
    assign lead_off_register_index=lead_off_ctrl_register_index;assign lead_off_error_adc=lead_off_ctrl_error_adc;
    assign lead_off_error_register_address=lead_off_ctrl_error_address;
    assign lead_off_error_expected=lead_off_ctrl_error_expected;assign lead_off_error_actual=lead_off_ctrl_error_actual;
    assign lead_off_status_word={21'd0,id_gate_passed,lead_off_ctrl_resumed,lead_off_ctrl_restore_adc2,
        lead_off_ctrl_restore_adc1,(lead_off_ctrl_phase==7),(controller_state==29),lead_off_unknown,
        lead_off_failed,lead_off_done,lead_off_busy};
    assign lead_off_error_word={(lead_off_ctrl_error_code!=0),6'd0,lead_off_ctrl_error_adc,
        lead_off_ctrl_error_address,lead_off_ctrl_error_expected,lead_off_ctrl_error_actual};
    assign lead_off_sample_counts={lead_off_ctrl_samples_adc2,lead_off_ctrl_samples_adc1};
    assign lead_off_off_masks={lead_off_ctrl_neg_adc2,lead_off_ctrl_pos_adc2,
        lead_off_ctrl_neg_adc1,lead_off_ctrl_pos_adc1};
    assign lead_off_owner_state={19'd0,(controller_state==29),data_window_active,
        controller_spi_busy,frame_rx_busy,frame_boundary_safe,controller_spi_operation,controller_state};
    assign lead_off_phase_word={22'd0,lead_off_ctrl_register_index,lead_off_ctrl_phase};

    // A normal user stop finishes the current packet (2**STOP_ALIGN_LOG2 frames: 32 for EDP2, 128 for EDP3). The pulse is
    // registered AFTER admission of sequence31, so the final frame is retained.
    always @(posedge acq_clk or negedge acq_rst_n) begin
        if(!acq_rst_n) begin chunk_stop_pending<=0;chunk_shutdown_pulse<=0;end
        else begin
            chunk_shutdown_pulse<=0;
            if(acq_shutdown_pulse) chunk_stop_pending<=1;
            if(chunk_stop_pending && (!data_window_active ||
                (production_pair_valid && production_pair_sequence[STOP_ALIGN_LOG2-1:0]=={STOP_ALIGN_LOG2{1'b1}}))) begin
                chunk_shutdown_pulse<=1;chunk_stop_pending<=0;
            end
        end
    end
    wire fifo_almost_full;
    wire fifo_gap;
    wire [31:0] fifo_overflow_count,fifo_dropped_count,fifo_partial_count,fifo_gap_count;
    wire [31:0] fifo_last_accepted,fifo_last_consumed,consume_sequence;
    wire [215:0] consume_ads1_frame,consume_ads2_frame;
    wire [23:0] head_ads1_status,head_ads2_status,consume_ads1_status,consume_ads2_status;
    assign quarantine_active=acq_fault_pulse|lease_expired|controller_fault_pending|clock_loss_latched|controller_fault_safe;
    assign fifo_shutdown_req=chunk_shutdown_pulse|controller_shutdown_safe;
    // Quarantine is immediate. Existing occupancy is retained but all
    // snapshot, consume, and write admission is locked.
    assign fifo_fault_req=quarantine_active;
    assign fifo_write_accept=production_pair_valid&&id_gate_passed&&!fifo_stopped&&!fifo_shutdown_req&&
        !fifo_fault_req&&!fifo_shutdown_safe&&!fifo_fault_safe&&!fifo_full;
    assign fifo_read_accept=fifo_consume_req&&!fifo_empty&&!fifo_shutdown_safe&&!fifo_fault_safe&&!fifo_fault_req;
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

    wire token_error_sticky; wire [31:0] token_error_count;
    wire [23:0] snapshot_ads1_status,snapshot_ads2_status;
    // DMA is the sole owner for this candidate. Mode changes require a full
    // coordinated platform reset; there is deliberately no runtime mux.
    wire dma_prepared; wire [31:0] dma_frames,dma_stalls,dma_status;
    wire heartbeat_toggle;
    reg [31:0] lease_cycles=0;
    // acq_clk == AXI clock by the existing clock contract. Single writer.
    // Independent 2-second refresh lease. No fixed acquisition duration here;
    // the approved host/driver scope owns any per-session time limit.
    // Retires via the existing fault/quarantine/PHY-drain path, never re-ARM.
    always @(posedge acq_clk or negedge acq_rst_n) begin
        if(!acq_rst_n) begin
            heartbeat_seen<=0;lease_expired<=0;lease_cycles<=0;
        end else if(dma_prepared && !lease_expired) begin
            if(heartbeat_toggle!=heartbeat_seen) begin heartbeat_seen<=heartbeat_toggle;lease_cycles<=0;end
            else if(lease_cycles==199999999) lease_expired<=1;
            else lease_cycles<=lease_cycles+1;
        end
    end
    assign snapshot_valid=1'b0;assign snapshot_token=0;assign snapshot_sequence=0;
    assign snapshot_ads1_frame=0;assign snapshot_ads2_frame=0;
    assign snapshot_ads1_status=0;assign snapshot_ads2_status=0;
    assign token_error_sticky=0;assign token_error_count=0;
    axis_cont_packetizer_terminal_v1 #(.SAMPLE_INTERVAL_NS(SAMPLE_INTERVAL_NS)) packetizer(
        .clk(acq_clk),.rst_n(acq_rst_n),.prepared(dma_prepared),
        .fault_req(fifo_fault_req|frame_metadata_fault),.head_valid(head_valid && head_metadata_valid),.head_sequence(head_sequence),
        .head_frame_cycle(frame_cycles[head_sequence[7:0]]),.m_axis_frame_cycle(m_axis_frame_cycle),
        .head_ads1_frame(head_ads1_frame),.head_ads2_frame(head_ads2_frame),
        .fifo_pop(fifo_consume_req),.m_axis_tdata(m_axis_tdata),.m_axis_tkeep(m_axis_tkeep),
        .m_axis_tvalid(m_axis_tvalid),.m_axis_tready(m_axis_tready),.m_axis_tlast(m_axis_tlast),
        .frames_sent(dma_frames),.stall_cycles(dma_stalls),.stream_status(dma_status));
    assign unified_shutdown_safe=controller_shutdown_safe&&fifo_shutdown_safe;
    assign unified_fault_safe=controller_fault_safe&&fifo_fault_safe;

    assign parameter_contract_ok=(FRONT_SYS_CLK_HZ==100_000_000)&&
        (FRONT_ADS_CLK_HZ==2_000_000)&&(FRONT_HALF_PERIOD_CYCLES==25)&&
        (FRONT_SCLK_HALF_DIV==50)&&(WINDOW_VALID_FRAMES==0)&&(FIFO_DEPTH==256);
    reg [EXTERNAL_STATUS_WIDTH-1:0] acq_status_live;
    wire [8:0] fifo_status_bits={token_error_sticky,snapshot_valid,fifo_fault_safe,
        fifo_shutdown_safe,fifo_stopped,fifo_overflow,fifo_almost_full,fifo_full,fifo_empty};
    always @* begin
        acq_status_live={EXTERNAL_STATUS_WIDTH{1'b0}};
        acq_status_live[4:0]=controller_state;acq_status_live[6]=id_gate_passed;acq_status_live[7]=config_passed;
        acq_status_live[8]=data_window_active;acq_status_live[9]=unified_shutdown_safe;
        acq_status_live[10]=unified_fault_safe;acq_status_live[26:11]=full_error_flags;
        acq_status_live[50:27]=ads1_id_reads;acq_status_live[74:51]=ads2_id_reads;
        acq_status_live[85:75]=ads1_config_pass_bitmap[10:0];acq_status_live[96:86]=ads2_config_pass_bitmap[10:0];
        acq_status_live[2047:2016]=ads1_config_readbacks[119:88];
        acq_status_live[2079:2048]=ads2_config_readbacks[119:88];
        acq_status_live[2083:2080]=ads1_config_pass_bitmap[14:11];
        acq_status_live[2087:2084]=ads2_config_pass_bitmap[14:11];
        acq_status_live[128:97]=complete_pair_count;acq_status_live[160:129]=discarded_settling_count;
        acq_status_live[192:161]=partial_frame_count;acq_status_live[224:193]=drdy_error_count;
        acq_status_live[256:225]=spi_error_count;acq_status_live[280:257]=last_ads1_status;
        acq_status_live[304:281]=last_ads2_status;acq_status_live[312:305]=data_window_summary;
        acq_status_live[344:313]=watchdog_counter;acq_status_live[352:345]=shutdown_reason;
        acq_status_live[440:353]=ads1_config_readbacks[87:0];acq_status_live[528:441]=ads2_config_readbacks[87:0];
        acq_status_live[560:529]=total_drdy_event_count;acq_status_live[592:561]=frame_timeout_count;
        acq_status_live[624:593]=frame_length_error_count;acq_status_live[656:625]=status_header_error_count;
        acq_status_live[672:657]=valid_channel_positions;acq_status_live[676:673]=config_status;
        acq_status_live[677]=frame_boundary_safe;acq_status_live[679:678]=accepted_event_count;
        acq_status_live[680]=shutdown_req_seen;acq_status_live[872:681]=last_ads1_channels;
        acq_status_live[1064:873]=last_ads2_channels;acq_status_live[UF_FIFO_STATUS_LSB+:9]=fifo_status_bits;
        acq_status_live[1065]=lead_off_unknown;
        acq_status_live[2151:2088]=cycle_counter;
        acq_status_live[UF_OCCUPANCY_LSB+:32]=fifo_occupancy;acq_status_live[UF_CAPACITY_LSB+:32]=FIFO_DEPTH;
        acq_status_live[UF_PRODUCER_LSB+:32]=production_pair_sequence;acq_status_live[UF_CONSUMER_LSB+:32]=fifo_last_consumed;
        acq_status_live[UF_OVERFLOW_LSB+:32]=fifo_overflow_count;acq_status_live[UF_DROPPED_LSB+:32]=fifo_dropped_count;
        acq_status_live[UF_PARTIAL_LSB+:32]=fifo_partial_count;acq_status_live[UF_GAP_LSB+:32]=fifo_gap_count;
        acq_status_live[UF_LAST_ACCEPTED_LSB+:32]=fifo_last_accepted;acq_status_live[UF_LAST_CONSUMED_LSB+:32]=fifo_last_consumed;
        acq_status_live[UF_TOKEN_LSB+:32]=snapshot_token;acq_status_live[UF_SNAPSHOT_VALID_BIT]=snapshot_valid;
        acq_status_live[UF_SNAPSHOT_SEQUENCE_LSB+:32]=snapshot_sequence;
        acq_status_live[UF_SNAPSHOT_ADS1_STATUS_LSB+:24]=snapshot_ads1_status;
        acq_status_live[UF_SNAPSHOT_ADS2_STATUS_LSB+:24]=snapshot_ads2_status;
        acq_status_live[UF_SNAPSHOT_ADS1_FRAME_LSB+:216]=snapshot_ads1_frame;
        acq_status_live[UF_SNAPSHOT_ADS2_FRAME_LSB+:216]=snapshot_ads2_frame;
        acq_status_live[UF_TOKEN_ERROR_LSB+:32]=token_error_count;
    end
    unified_cdc_v2 #(.STATUS_WIDTH(EXTERNAL_STATUS_WIDTH)) unified_cdc(
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
    cont_dma_regs_leadoff_terminal_v1 #(.STATUS_WIDTH(EXTERNAL_STATUS_WIDTH)) unified_regs(
        .s_axi_aclk(axi_clk),.s_axi_aresetn(axi_rst_n),.s_axi_awaddr(s_axi_awaddr),
        .s_axi_awvalid(s_axi_awvalid),.s_axi_awready(s_axi_awready),.s_axi_wdata(s_axi_wdata),
        .s_axi_wstrb(s_axi_wstrb),.s_axi_wvalid(s_axi_wvalid),.s_axi_wready(s_axi_wready),
        .s_axi_bresp(s_axi_bresp),.s_axi_bvalid(s_axi_bvalid),.s_axi_bready(s_axi_bready),
        .s_axi_araddr(s_axi_araddr),.s_axi_arvalid(s_axi_arvalid),.s_axi_arready(s_axi_arready),
        .s_axi_rdata(s_axi_rdata),.s_axi_rresp(s_axi_rresp),.s_axi_rvalid(s_axi_rvalid),.s_axi_rready(s_axi_rready),
        .dma_prepared(dma_prepared),.heartbeat_toggle(heartbeat_toggle),.dma_frames(dma_frames),.dma_stalls(dma_stalls),
        .dma_status(dma_status),.status_snapshot(axi_status_snapshot),.arm_ack_toggle(arm_ack_t),
        .shutdown_ack_toggle(shutdown_ack_t),.clear_ack_toggle(clear_ack_t),
        .snapshot_ack_toggle(status_snapshot_ack_t),.fifo_snapshot_ack_toggle(fifo_snapshot_ack_t),
        .consume_ack_toggle(consume_ack_t),.fault_ack_toggle(fault_ack_t),
        .arm_request_toggle(arm_req_t),.shutdown_request_toggle(shutdown_req_t),
        .clear_request_toggle(clear_req_t),.snapshot_request_toggle(status_snapshot_req_t),
        .fifo_snapshot_request_toggle(fifo_snapshot_req_t),.consume_request_toggle(consume_req_t),
        .fault_request_toggle(fault_req_t),.lead_off_busy(lead_off_busy),.lead_off_done(lead_off_done),
        .lead_off_failed(lead_off_failed),.lead_off_unknown(lead_off_unknown),
        .initialize_status(initialize_status),.initialize_request(initialize_request),
        .initialize_admission_allowed(controller_state==0 && !lead_off_unknown && !controller_fault_pending &&
            !system_fault_latched && !clock_loss_latched),
        .arm_admission_allowed(controller_state==30 && id_gate_passed && config_passed &&
            !lead_off_busy && !lead_off_unknown && !controller_fault_pending && !system_fault_latched),
        .lead_off_status_word(lead_off_status_word),.lead_off_error_code(lead_off_error_code),
        .lead_off_error_word(lead_off_error_word),.lead_off_sample_counts(lead_off_sample_counts),
        .lead_off_off_masks(lead_off_off_masks),.lead_off_owner_state(lead_off_owner_state),
        .calibration_maintenance_admitted(controller_state==30&&!data_window_active&&!dma_prepared&&
            !lead_off_unknown&&!controller_fault_pending&&!system_fault_latched&&frame_boundary_safe),
        .calibration_config(calibration_config),.calibration_read_index(calibration_read_index),
        .calibration_result_generation(calibration_generation),.calibration_sample_count(calibration_sample_count),
        .calibration_bound_config(calibration_bound_config),.calibration_result_flags(calibration_result_flags),
        .calibration_read_sample(calibration_read_sample),.calibration_start_request(calibration_start_request),
        .lead_off_phase_word(lead_off_phase_word),.lead_off_start_request(lead_off_start_request),
        .lead_off_cancel_request(lead_off_cancel_request),
        .lead_off_acknowledge_request(lead_off_acknowledge_request));
endmodule
