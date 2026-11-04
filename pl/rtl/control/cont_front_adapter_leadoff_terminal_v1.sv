`timescale 1ns/1ps

// Explicit-interface front adapter derived from the accepted 4J/4K exit candidate.
// The production 4D controller and accepted candidates remain unchanged.
// controller source remains unchanged. This adapter instantiates a separate
// source-derived candidate because the frozen controller has no loss input.
//
// clock_loss_in is a synchronous fault-injection input. The independent
// candidate controller latches it, blocks PHY acceptance on that same edge,
// drains already accepted transfers under PL clk, and enters real FAULT_SAFE.
// No reset substitution or state masking is used for clock-loss handling.
module cont_front_adapter_leadoff_terminal_v1 #(
    parameter integer SYS_CLK_HZ = 100_000_000,
    parameter integer HALF_PERIOD_CYCLES = 25,
    parameter integer CTRL_SYS_CLK_HZ = 100_000_000,
    parameter integer CTRL_ADS_CLK_HZ = 2_000_000,
    parameter integer TPOR_WAIT_CYCLES = 21_000_000,
    parameter integer VCAP_SETTLE_WAIT_CYCLES = 100_000_000,
    parameter integer RESET_LOW_CYCLES = 100_000,
    parameter integer RESET_RELEASE_WAIT_CYCLES = 100_000,
    parameter integer SPI_PREPARE_CYCLES = 200,
    parameter integer SPI_TIMEOUT_CYCLES = 100_000,
    parameter integer EXIT_TIMEOUT_CYCLES = SPI_TIMEOUT_CYCLES,
    parameter integer START_ASSERT_CYCLES = 200,
    parameter integer PARTIAL_TIMEOUT_CYCLES = 400_000,
    parameter integer DATA_FRAME_TIMEOUT_CYCLES = 2_000_000,
    parameter integer WATCHDOG_CYCLES = 2_000_000_000,
    parameter integer WINDOW_VALID_FRAMES = 0,
    parameter integer ADS_CLK_HZ = 2_000_000,
    parameter logic [7:0] CONFIG1_BYTE = 8'h96,
    parameter integer SCLK_HALF_DIV = 50
) (

    input wire [31:0] calibration_config,input wire [9:0] calibration_read_index,
    output wire signed [31:0] calibration_read_sample,
    output wire [31:0] calibration_generation,calibration_sample_count,
    output wire [31:0] calibration_bound_config,calibration_result_flags,
    input wire clk,
    input wire rst_n,
    input wire supply_valid,
    input wire clock_start_req,
    input wire clock_loss_in,
    input wire exit_comm_fault_req,
    input wire system_fault_req,
    input wire controller_arm_req,
    input wire controller_initialize_req,
    input wire software_shutdown_req,
    input wire protocol_fault_req,
    input wire illegal_cs_fault_req,
    input wire lead_off_start,input wire lead_off_cancel,input wire lead_off_acknowledge,
    input wire adc1_dout,
    input wire adc2_dout,
    input wire adc1_drdy_n,
    input wire adc2_drdy_n,
    output wire adc_clk,
    output wire [2:0] adc_clk_state,
    output wire adc_clk_shutdown_safe,
    output wire adc_clk_fault_safe,
    output wire adc_clk_start_accepted,
    output wire adc_clk_running,
    output wire [4:0] controller_state,
    output wire controller_busy,
    output wire controller_shutdown_safe,
    output wire controller_fault_safe,
    output wire [6:0] control_outputs,
    output wire adc_pwdn_n,
    output wire adc_reset_n,
    output wire adc_sclk,
    output wire adc_din,
    output wire adc_start,
    output wire adc1_cs_n,
    output wire adc2_cs_n,
    output wire [23:0] ads1_id_reads,
    output wire [23:0] ads2_id_reads,
    output wire [7:0] id_diagnostic_flags,
    output wire [3:0] id_summary,
    output wire id_gate_passed,
    output wire [15:0] full_error_flags,
    output wire [1:0] accepted_event_count,
    output wire data_window_active,
    output wire data_window_passed,
    output wire clock_loss_latched,
    output wire clock_loss_fault_safe,
    output wire clock_loss_inflight_at_detection,
    output wire [31:0] clock_loss_response_cycles,
    output wire [31:0] transactions_accepted_before_loss,
    output wire [31:0] transactions_accepted_after_loss,
    output wire controller_spi_busy,
    output wire controller_spi_start,
    output wire [2:0] controller_spi_operation,
    output wire controller_spi_done,
    output wire controller_command_accept,
    output wire controller_frame_accept,
    output wire controller_fault_pending,
    output wire system_fault_latched,
    output wire [1:0] exit_accept_mask,
    output wire [1:0] exit_phy_done_mask,
    output wire [1:0] exit_success_mask,
    output wire exit_comm_fault_latched,
    output wire exit_timeout_latched,
    output wire [31:0] exit_abort_response_cycles,
    output wire config_passed,
    output wire [3:0] config_status,
    output wire [119:0] ads1_config_readbacks,
    output wire [119:0] ads2_config_readbacks,
    output wire [14:0] ads1_config_pass_bitmap,
    output wire [14:0] ads2_config_pass_bitmap,
    output wire frame_boundary_safe,
    output wire frame_rx_busy,
    output wire [31:0] complete_pair_count,
    output wire [31:0] discarded_settling_count,
    output wire [31:0] partial_frame_count,
    output wire [31:0] drdy_error_count,
    output wire [31:0] spi_error_count,
    output wire [31:0] total_drdy_event_count,
    output wire [31:0] frame_timeout_count,
    output wire [31:0] frame_length_error_count,
    output wire [31:0] status_header_error_count,
    output wire [23:0] last_ads1_status,
    output wire [23:0] last_ads2_status,
    output wire [191:0] last_ads1_channels,
    output wire [191:0] last_ads2_channels,
    output wire [15:0] valid_channel_positions,
    output wire [7:0] data_window_summary,
    output wire [31:0] watchdog_counter,
    output wire shutdown_req_seen,
    output wire production_pair_valid,
    output wire [31:0] production_pair_sequence,
    output wire [215:0] production_ads1_frame,
    output wire [215:0] production_ads2_frame,
    output wire production_partial_event,
    output wire [1:0] production_partial_mask,
    output wire [7:0] shutdown_reason,
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
    localparam [2:0] CLK_ST_RUN = 3'd1;
    localparam [4:0] CTRL_ST_ARM_SAFE = 5'd0;
    localparam [4:0] CTRL_ST_FAULT_SAFE = 5'd27;

    reg startup_shutdown_latched;
    wire clock_start_authorized = supply_valid && rst_n && clock_start_req &&
        !clock_loss_latched && !clock_loss_in && !system_fault_req &&
        !system_fault_latched && !software_shutdown_req && !startup_shutdown_latched;
    wire controller_clock_ready;
    reg adc_clk_sampled;
    reg adc_clk_first_edge_seen;
    reg controller_arm_pending;
    reg controller_initialize_pending;

    reg clock_loss_inflight_at_detection_reg;

    reg [31:0] transactions_before_loss_reg;
    reg [31:0] transactions_after_loss_reg;
    reg transaction_in_flight;

    wire [4:0] controller_state_raw;
    wire controller_busy_raw, controller_shutdown_safe_raw, controller_fault_safe_raw;
    wire [6:0] control_outputs_raw;
    wire adc_pwdn_n_raw, adc_reset_n_raw, adc_sclk_raw, adc_din_raw, adc_start_raw;
    wire adc1_cs_n_raw, adc2_cs_n_raw;
    wire [23:0] ads1_id_reads_raw, ads2_id_reads_raw;
    wire [7:0] id_diagnostic_flags_raw;
    wire [3:0] id_summary_raw;
    wire id_gate_passed_raw;
    wire [15:0] full_error_flags_raw;
    wire [1:0] accepted_event_count_raw;
    wire data_window_active_raw, data_window_passed_raw;
    wire controller_spi_busy_raw, controller_spi_done_raw, controller_spi_start_raw;
    wire [2:0] controller_spi_operation_raw;
    wire [7:0] shutdown_reason_raw;

    wire clock_loss_active = clock_loss_latched || clock_loss_in;
    wire [31:0] drain_bound;
    wire frame_busy;
    wire controller_rst_n = rst_n;
    wire controller_arm_pulse = controller_arm_pending && controller_clock_ready &&
        (controller_state_raw == CTRL_ST_ARM_SAFE || controller_state_raw==5'd30) && !clock_loss_active &&
        !system_fault_req && !system_fault_latched &&
        !software_shutdown_req && !startup_shutdown_latched;
    wire controller_protocol_fault = protocol_fault_req;
    wire candidate_shutdown_req = controller_shutdown_safe_raw || startup_shutdown_latched ||
        (software_shutdown_req && controller_state_raw == CTRL_ST_ARM_SAFE);
    wire candidate_fault_req = controller_fault_safe_raw || clock_loss_active;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin

            clock_loss_inflight_at_detection_reg <= 1'b0;

            transactions_before_loss_reg <= 32'd0;
            transactions_after_loss_reg <= 32'd0;
            transaction_in_flight <= 1'b0;
        end else begin
            if (controller_command_accept || controller_frame_accept) begin
                transaction_in_flight <= 1'b1;
                if (clock_loss_active)
                    transactions_after_loss_reg <= transactions_after_loss_reg + 1'b1;
                else
                    transactions_before_loss_reg <= transactions_before_loss_reg + 1'b1;
            end
            if (controller_spi_done_raw || !frame_busy)
                transaction_in_flight <= 1'b0;
            if (clock_loss_in && !clock_loss_latched) begin
                clock_loss_inflight_at_detection_reg <= controller_spi_busy_raw || frame_busy;
            end
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            adc_clk_sampled <= 1'b0;
            adc_clk_first_edge_seen <= 1'b0;
        end else if (clock_loss_active || adc_clk_state != CLK_ST_RUN) begin
            adc_clk_sampled <= 1'b0;
            adc_clk_first_edge_seen <= 1'b0;
        end else begin
            adc_clk_sampled <= adc_clk;
            if (adc_clk && !adc_clk_sampled)
                adc_clk_first_edge_seen <= 1'b1;
        end
    end

    assign adc_clk_running = adc_clk_state == CLK_ST_RUN;
    assign controller_clock_ready = adc_clk_running && adc_clk_first_edge_seen;

    assign clock_loss_fault_safe = clock_loss_latched && controller_fault_safe_raw;
    assign clock_loss_inflight_at_detection = clock_loss_inflight_at_detection_reg;

    assign transactions_accepted_before_loss = transactions_before_loss_reg;
    assign transactions_accepted_after_loss = transactions_after_loss_reg;

    adc_clk_start_authorized_candidate #(
        .SYS_CLK_HZ(SYS_CLK_HZ), .HALF_PERIOD_CYCLES(HALF_PERIOD_CYCLES)
    ) u_adc_clk (
        .clk(clk), .rst_n(rst_n), .start_authorized(clock_start_authorized),
        .shutdown_req(candidate_shutdown_req), .fault_req(candidate_fault_req),
        .adc_clk(adc_clk), .state(adc_clk_state),
        .shutdown_safe(adc_clk_shutdown_safe), .fault_safe(adc_clk_fault_safe),
        .start_accepted(adc_clk_start_accepted), .divider_count()
    );

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            controller_arm_pending <= 1'b0;
            controller_initialize_pending <= 1'b0;
            startup_shutdown_latched <= 1'b0;
        end else if (clock_loss_active) begin
            controller_arm_pending <= 1'b0;
            controller_initialize_pending <= 1'b0;
        end
        else if (software_shutdown_req && controller_state_raw == CTRL_ST_ARM_SAFE) begin
            startup_shutdown_latched <= 1'b1;
            controller_arm_pending <= 1'b0;
            controller_initialize_pending <= 1'b0;
        end
        else begin
            if (controller_state_raw != CTRL_ST_ARM_SAFE && controller_state_raw!=5'd30) begin
                controller_arm_pending <= 1'b0;
                controller_initialize_pending <= 1'b0;
            end
            else if (controller_arm_req || controller_initialize_req)begin
                controller_arm_pending <= 1'b1;
                controller_initialize_pending <= controller_initialize_req;
            end
            if (controller_arm_pulse) begin
                controller_arm_pending <= 1'b0;
                controller_initialize_pending <= 1'b0;
            end
        end
    end

    cont_fault_ctrl_leadoff_terminal_v1 #(
        .TPOR_WAIT_CYCLES(TPOR_WAIT_CYCLES),
        .VCAP_SETTLE_WAIT_CYCLES(VCAP_SETTLE_WAIT_CYCLES),
        .RESET_LOW_CYCLES(RESET_LOW_CYCLES),
        .RESET_RELEASE_WAIT_CYCLES(RESET_RELEASE_WAIT_CYCLES),
        .SPI_PREPARE_CYCLES(SPI_PREPARE_CYCLES),
        .SPI_TIMEOUT_CYCLES(SPI_TIMEOUT_CYCLES),
        .EXIT_TIMEOUT_CYCLES(EXIT_TIMEOUT_CYCLES),
        .START_ASSERT_CYCLES(START_ASSERT_CYCLES),
        .PARTIAL_TIMEOUT_CYCLES(PARTIAL_TIMEOUT_CYCLES),
        .DATA_FRAME_TIMEOUT_CYCLES(DATA_FRAME_TIMEOUT_CYCLES),
        .WATCHDOG_CYCLES(WATCHDOG_CYCLES),
        .WINDOW_VALID_FRAMES(WINDOW_VALID_FRAMES),
        .CONFIG1_BYTE(CONFIG1_BYTE),
        .SYS_CLK_HZ(CTRL_SYS_CLK_HZ), .ADS_CLK_HZ(CTRL_ADS_CLK_HZ),
        .SCLK_HALF_DIV(SCLK_HALF_DIV)
    ) u_controller (
        .clk(clk), .rst_n(controller_rst_n), .arm_start_req(controller_arm_pulse),
        .initialize_only(controller_initialize_pending),
        .software_shutdown_req(software_shutdown_req), .system_fault_req(system_fault_req),
        .clear_sticky_req(1'b0), .exit_comm_fault_in(exit_comm_fault_req),
        .exit_comm_fault_latched(exit_comm_fault_latched),
        .exit_timeout_latched(exit_timeout_latched),
        .exit_accept_mask(exit_accept_mask), .exit_phy_done_mask(exit_phy_done_mask),
        .exit_success_mask(exit_success_mask),
        .exit_abort_response_cycles(exit_abort_response_cycles),
        .clock_loss_in(clock_loss_in), .clock_loss_latched(clock_loss_latched),
        .system_fault_latched(system_fault_latched),
        .controller_fault_pending(controller_fault_pending),
        .clock_loss_response_cycles(clock_loss_response_cycles),
        .command_accept(controller_command_accept), .frame_accept(controller_frame_accept),
        .clock_loss_drain_bound_cycles(drain_bound),
        .protocol_fault_in(controller_protocol_fault),
        .illegal_cs_fault_in(illegal_cs_fault_req), .test_data_window_error_in(1'b0),
        
        .calibration_config(calibration_config),.calibration_read_index(calibration_read_index),
        .calibration_read_sample(calibration_read_sample),.calibration_generation(calibration_generation),
        .calibration_sample_count(calibration_sample_count),.calibration_bound_config(calibration_bound_config),
        .calibration_result_flags(calibration_result_flags),
        .lead_off_start(lead_off_start),.lead_off_cancel(lead_off_cancel),
        .lead_off_acknowledge(lead_off_acknowledge),.lead_off_busy(lead_off_busy),
        .lead_off_done(lead_off_done),.lead_off_failed(lead_off_failed),
        .lead_off_unknown(lead_off_unknown),.lead_off_error_code(lead_off_error_code),
        .lead_off_status_samples_adc1(lead_off_status_samples_adc1),
        .lead_off_status_samples_adc2(lead_off_status_samples_adc2),
        .lead_off_positive_off_mask_adc1(lead_off_positive_off_mask_adc1),
        .lead_off_negative_off_mask_adc1(lead_off_negative_off_mask_adc1),
        .lead_off_positive_off_mask_adc2(lead_off_positive_off_mask_adc2),
        .lead_off_negative_off_mask_adc2(lead_off_negative_off_mask_adc2),
        .lead_off_restore_verified_adc1(lead_off_restore_verified_adc1),
        .lead_off_restore_verified_adc2(lead_off_restore_verified_adc2),
        .lead_off_continuous_resumed(lead_off_continuous_resumed),.lead_off_phase(lead_off_phase),
        .lead_off_register_index(lead_off_register_index),.lead_off_error_adc(lead_off_error_adc),
        .lead_off_error_register_address(lead_off_error_register_address),
        .lead_off_error_expected(lead_off_error_expected),.lead_off_error_actual(lead_off_error_actual),
        .adc1_dout(adc1_dout), .adc2_dout(adc2_dout),
        .adc1_drdy_n(adc1_drdy_n), .adc2_drdy_n(adc2_drdy_n),
        .state(controller_state_raw), .counter(), .busy(controller_busy_raw),
        .done(), .shutdown_safe(controller_shutdown_safe_raw),
        .fault_safe(controller_fault_safe_raw),
        .accepted_event_count(accepted_event_count_raw), .synchronized_inputs(),
        .control_outputs(control_outputs_raw), .adc_din(adc_din_raw),
        .adc_pwdn_n(adc_pwdn_n_raw), .adc_reset_n(adc_reset_n_raw),
        .adc_sclk(adc_sclk_raw), .adc_start(adc_start_raw),
        .adc1_cs_n(adc1_cs_n_raw), .adc2_cs_n(adc2_cs_n_raw),
        .spi_busy(controller_spi_busy_raw), .spi_done(controller_spi_done_raw),
        .spi_start(controller_spi_start_raw), .spi_operation(controller_spi_operation_raw),
        .spi_transfer_bits(), .ads1_id_raw(), .ads2_id_raw(), .id_valid(),
        .ads1_id_reads(ads1_id_reads_raw), .ads2_id_reads(ads2_id_reads_raw),
        .id_diagnostic_flags(id_diagnostic_flags_raw), .id_summary(id_summary_raw),
        .id_gate_passed(id_gate_passed_raw),
        .ads1_config_readbacks(ads1_config_readbacks),
        .ads2_config_readbacks(ads2_config_readbacks),
        .ads1_config_pass_bitmap(ads1_config_pass_bitmap),
        .ads2_config_pass_bitmap(ads2_config_pass_bitmap),
        .config_passed(config_passed), .config_status(config_status),
        .current_config_address(), .current_config_index(), .current_config_device(),
        .data_window_active(data_window_active_raw), .data_window_passed(data_window_passed_raw),
        .data_window_summary(data_window_summary), .frame_rx_busy(frame_busy),
        .frame_boundary_safe(frame_boundary_safe), .drdy_pair_state(), .frame_received_bits(),
        .total_drdy_event_count(total_drdy_event_count),
        .complete_pair_count(complete_pair_count),
        .discarded_settling_count(discarded_settling_count),
        .partial_frame_count(partial_frame_count), .drdy_error_count(drdy_error_count),
        .frame_timeout_count(frame_timeout_count), .spi_error_count(spi_error_count),
        .frame_length_error_count(frame_length_error_count),
        .status_header_error_count(status_header_error_count),
        .last_ads1_status(last_ads1_status), .last_ads2_status(last_ads2_status),
        .last_ads1_channels(last_ads1_channels), .last_ads2_channels(last_ads2_channels),
        .valid_channel_positions(valid_channel_positions),
        .production_pair_valid(production_pair_valid),
        .production_pair_sequence(production_pair_sequence),
        .production_ads1_frame(production_ads1_frame),
        .production_ads2_frame(production_ads2_frame),
        .production_partial_event(production_partial_event),
        .production_partial_mask(production_partial_mask),
        .full_error_flags(full_error_flags_raw), .error_flags(),
        .transport_error_flags(), .register_error_flags(), .test_error_flags(),
        .shutdown_reason(shutdown_reason_raw), .shutdown_req_seen(shutdown_req_seen),
        .watchdog_counter(watchdog_counter)
    );

    // Expose the real candidate controller state and unmasked PHY outputs.
    assign controller_state = controller_state_raw;
    assign controller_busy = controller_busy_raw;
    assign controller_shutdown_safe = controller_shutdown_safe_raw;
    assign controller_fault_safe = controller_fault_safe_raw;
    assign control_outputs = control_outputs_raw;
    assign adc_pwdn_n = adc_pwdn_n_raw;
    assign adc_reset_n = adc_reset_n_raw;
    assign adc_sclk = adc_sclk_raw;
    assign adc_din = adc_din_raw;
    assign adc_start = adc_start_raw;
    assign adc1_cs_n = adc1_cs_n_raw;
    assign adc2_cs_n = adc2_cs_n_raw;
    assign ads1_id_reads = ads1_id_reads_raw;
    assign ads2_id_reads = ads2_id_reads_raw;
    assign id_diagnostic_flags = id_diagnostic_flags_raw;
    assign id_summary = id_summary_raw;
    assign id_gate_passed = id_gate_passed_raw;
    assign full_error_flags = full_error_flags_raw;
    assign accepted_event_count = accepted_event_count_raw;
    assign data_window_active = data_window_active_raw;
    assign data_window_passed = data_window_passed_raw;
    assign controller_spi_busy = controller_spi_busy_raw;
    assign controller_spi_start = controller_spi_start_raw;
    assign controller_spi_operation = controller_spi_operation_raw;
    assign controller_spi_done = controller_spi_done_raw;
    assign frame_rx_busy = frame_busy;
    assign shutdown_reason = shutdown_reason_raw;
endmodule
