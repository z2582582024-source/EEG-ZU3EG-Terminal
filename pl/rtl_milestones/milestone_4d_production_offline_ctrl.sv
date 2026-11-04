`timescale 1ns/1ps

// Milestone 4D-PRODUCTION-OFFLINE controller.
// Offline integration candidate only: hardware execution remains prohibited.
module milestone_4d_production_offline_ctrl #(
    parameter logic [31:0] TPOR_WAIT_CYCLES          = 32'd21_000_000,
    parameter logic [31:0] VCAP_SETTLE_WAIT_CYCLES   = 32'd100_000_000,
    parameter logic [31:0] RESET_LOW_CYCLES          = 32'd100_000,
    parameter logic [31:0] RESET_RELEASE_WAIT_CYCLES = 32'd100_000,
    parameter logic [31:0] SPI_PREPARE_CYCLES        = 32'd196,
    parameter logic [31:0] SPI_TIMEOUT_CYCLES        = 32'd100_000,
    parameter logic [31:0] START_ASSERT_CYCLES       = 32'd196,
    parameter logic [31:0] PARTIAL_TIMEOUT_CYCLES    = 32'd400_000,
    parameter logic [31:0] DATA_FRAME_TIMEOUT_CYCLES = 32'd2_000_000,
    parameter logic [31:0] WATCHDOG_CYCLES           = 32'd2_000_000_000,
    parameter integer WINDOW_VALID_FRAMES            = 8,
    parameter integer SYS_CLK_HZ                     = 100_000_000,
    parameter integer ADS_CLK_HZ                     = 2_048_000,
    parameter integer SCLK_HALF_DIV                  = 50
) (
    input  wire clk,
    input  wire rst_n,
    input  wire arm_start_req,
    input  wire software_shutdown_req,
    input  wire clear_sticky_req,
    input  wire protocol_fault_in,
    input  wire illegal_cs_fault_in,
    input  wire test_data_window_error_in,
    input  wire adc1_dout,
    input  wire adc2_dout,
    input  wire adc1_drdy_n,
    input  wire adc2_drdy_n,

    output wire [4:0] state,
    output reg  [31:0] counter,
    output wire busy,
    output wire done,
    output wire shutdown_safe,
    output wire fault_safe,
    output reg  [1:0] accepted_event_count,
    output wire [3:0] synchronized_inputs,
    output wire [6:0] control_outputs,
    output wire adc_din,
    output wire adc_pwdn_n,
    output wire adc_reset_n,
    output wire adc_sclk,
    output wire adc_start,
    output wire adc1_cs_n,
    output wire adc2_cs_n,

    output wire spi_busy,
    output wire spi_done,
    output reg  spi_start,
    output reg  [2:0] spi_operation,
    output reg  [8:0] spi_transfer_bits,
    output reg  [7:0] ads1_id_raw,
    output reg  [7:0] ads2_id_raw,
    output wire [1:0] id_valid,
    output reg  [23:0] ads1_id_reads,
    output reg  [23:0] ads2_id_reads,
    output wire [7:0] id_diagnostic_flags,
    output wire [3:0] id_summary,
    output wire id_gate_passed,

    output reg  [87:0] ads1_config_readbacks,
    output reg  [87:0] ads2_config_readbacks,
    output reg  [10:0] ads1_config_pass_bitmap,
    output reg  [10:0] ads2_config_pass_bitmap,
    output wire config_passed,
    output wire [3:0] config_status,
    output reg  [7:0] current_config_address,
    output reg  [3:0] current_config_index,
    output reg  current_config_device,

    output wire data_window_active,
    output reg  data_window_passed,
    output wire [7:0] data_window_summary,
    output wire frame_rx_busy,
    output wire frame_boundary_safe,
    output wire [1:0] drdy_pair_state,
    output wire [8:0] frame_received_bits,
    output wire [31:0] total_drdy_event_count,
    output reg  [31:0] complete_pair_count,
    output reg  [31:0] discarded_settling_count,
    output wire [31:0] partial_frame_count,
    output wire [31:0] drdy_error_count,
    output wire [31:0] frame_timeout_count,
    output reg  [31:0] spi_error_count,
    output reg  [31:0] frame_length_error_count,
    output reg  [31:0] status_header_error_count,
    output reg  [23:0] last_ads1_status,
    output reg  [23:0] last_ads2_status,
    output reg  [191:0] last_ads1_channels,
    output reg  [191:0] last_ads2_channels,
    output reg  [15:0] valid_channel_positions,
    output wire production_pair_valid,
    output wire [31:0] production_pair_sequence,
    output wire [215:0] production_ads1_frame,
    output wire [215:0] production_ads2_frame,
    output wire production_partial_event,
    output wire [1:0] production_partial_mask,

    output reg  [15:0] full_error_flags,
    output wire [7:0] error_flags,
    output wire [7:0] transport_error_flags,
    output wire [7:0] register_error_flags,
    output wire [7:0] test_error_flags,
    output reg  [7:0] shutdown_reason,
    output reg  shutdown_req_seen,
    output reg  [31:0] watchdog_counter
);
    typedef enum logic [4:0] {
        ST_ARM_SAFE             = 5'd0,
        ST_STARTUP_TPOR_WAIT    = 5'd1,
        ST_VCAP_SETTLE_WAIT     = 5'd2,
        ST_RESET_PULSE_LOW      = 5'd3,
        ST_RESET_RELEASE_WAIT   = 5'd4,
        ST_SPI_PREPARE          = 5'd5,
        ST_ADS1_SDATAC          = 5'd6,
        ST_ADS1_ID_READ_1       = 5'd7,
        ST_ADS1_ID_READ_2       = 5'd8,
        ST_ADS1_ID_READ_3       = 5'd9,
        ST_ADS2_SDATAC          = 5'd10,
        ST_ADS2_ID_READ_1       = 5'd11,
        ST_ADS2_ID_READ_2       = 5'd12,
        ST_ADS2_ID_READ_3       = 5'd13,
        ST_ID_CONFIRM           = 5'd14,
        ST_CONFIG_WREG          = 5'd15,
        ST_CONFIG_RREG          = 5'd16,
        ST_RDATAC_ADS1          = 5'd17,
        ST_RDATAC_ADS2          = 5'd18,
        ST_START_ASSERT         = 5'd19,
        ST_WAIT_DRDY            = 5'd20,
        ST_FRAME_RECEIVE        = 5'd21,
        ST_TEST_DATA_WINDOW     = 5'd22,
        ST_EXIT_STOP            = 5'd23,
        ST_EXIT_SDATAC_ADS1     = 5'd24,
        ST_EXIT_SDATAC_ADS2     = 5'd25,
        ST_SHUTDOWN_SAFE        = 5'd26,
        ST_FAULT_SAFE           = 5'd27
    } state_t;

    localparam logic [2:0] SPI_OP_IDLE             = 3'd0;
    localparam logic [2:0] SPI_OP_SDATAC           = 3'd1;
    localparam logic [2:0] SPI_OP_ID_RREG          = 3'd2;
    localparam logic [2:0] SPI_OP_CONFIG_WREG      = 3'd3;
    localparam logic [2:0] SPI_OP_CONFIG_RREG      = 3'd4;
    localparam logic [2:0] SPI_OP_RDATAC           = 3'd5;
    localparam logic [2:0] SPI_OP_EXIT_SDATAC      = 3'd6;

    localparam logic [15:0] ERR_ID_GATE            = 16'h0001;
    localparam logic [15:0] ERR_SPI_TIMEOUT        = 16'h0002;
    localparam logic [15:0] ERR_SPI_PROTOCOL       = 16'h0004;
    localparam logic [15:0] ERR_ILLEGAL_CS         = 16'h0008;
    localparam logic [15:0] ERR_CONFIG_READBACK    = 16'h0010;
    localparam logic [15:0] ERR_DRDY_PAIR          = 16'h0020;
    localparam logic [15:0] ERR_PARTIAL_FRAME      = 16'h0040;
    localparam logic [15:0] ERR_FRAME_LENGTH       = 16'h0080;
    localparam logic [15:0] ERR_STATUS_HEADER      = 16'h0100;
    localparam logic [15:0] ERR_DATA_WINDOW        = 16'h0200;
    localparam logic [15:0] ERR_WATCHDOG           = 16'h0400;
    localparam logic [15:0] ERR_INVALID_STATE      = 16'h0800;

    localparam logic [7:0] REASON_NONE             = 8'h00;
    localparam logic [7:0] REASON_SOFTWARE         = 8'h01;
    localparam logic [7:0] REASON_TEST_COMPLETE    = 8'h02;
    localparam logic [7:0] REASON_WATCHDOG         = 8'h03;
    localparam logic [7:0] REASON_FAULT            = 8'h80;

    localparam logic [7:0] ADS1299_ID_MASK         = 8'h1F;
    localparam logic [7:0] ADS1299_ID_EXPECTED     = 8'h1E;

    (* fsm_encoding = "user_encoding", fsm_safe_state = "default_state" *)
    state_t state_reg = ST_ARM_SAFE;
    state_t state_next;
    reg [15:0] transition_error;
    reg operation_started;
    reg [1:0] spi_adc_mask;
    reg [215:0] spi_tx_bits;
    reg [31:0] spi_interbyte_wait_mask;
    reg [2:0] ads1_read_valid;
    reg [2:0] ads2_read_valid;
    reg shutdown_pending;
    reg fault_pending;
    reg exit_to_fault;
    reg spi_timeout_latched;
    reg watchdog_latched;
    reg [31:0] data_wait_timeout_count;
    reg [1:0] input_sync_0, input_sync_1, input_sync_2, input_sync_3;

    wire cmd_busy, cmd_done, cmd_sclk, cmd_din, cmd_adc1_cs_n, cmd_adc2_cs_n;
    wire [215:0] cmd_adc1_rx_bits, cmd_adc2_rx_bits;
    wire frame_sclk, frame_din, frame_adc1_cs_n, frame_adc2_cs_n;
    wire frame_valid, partial_frame_event, drdy_pair_event;
    wire [215:0] rx_adc1_frame, rx_adc2_frame;
    wire [31:0] rx_partial_count, rx_desync_count, rx_timeout_count;
    wire frame_enable;
    wire cmd_selected;
    wire data_selected;
    wire safe_boundary;
    wire watchdog_expired;
    wire shutdown_requested;
    wire ads1_consistent, ads2_consistent, ads1_all_expected, ads2_all_expected;

    function automatic [7:0] config_address(input [3:0] index);
        begin
            case (index)
                4'd0: config_address = 8'h01;
                4'd1: config_address = 8'h02;
                4'd2: config_address = 8'h03;
                4'd3: config_address = 8'h05;
                4'd4: config_address = 8'h06;
                4'd5: config_address = 8'h07;
                4'd6: config_address = 8'h08;
                4'd7: config_address = 8'h09;
                4'd8: config_address = 8'h0A;
                4'd9: config_address = 8'h0B;
                default: config_address = 8'h0C;
            endcase
        end
    endfunction

    function automatic [7:0] config_value(input [3:0] index);
        begin
            case (index)
                4'd0: config_value = 8'h96;
                4'd1: config_value = 8'hD0;
                4'd2: config_value = 8'hE0;
                default: config_value = 8'h05;
            endcase
        end
    endfunction

    function automatic [7:0] config_mask(input [3:0] index);
        begin
            config_mask = (index == 4'd2) ? 8'hFE : 8'hFF;
        end
    endfunction

    task automatic launch_spi(
        input logic [1:0] mask,
        input logic [8:0] bits,
        input logic [23:0] tx,
        input logic [31:0] wait_mask,
        input logic [2:0] op
    );
        begin
            spi_adc_mask <= mask;
            spi_transfer_bits <= bits;
            spi_tx_bits <= {{192{1'b0}}, tx};
            spi_interbyte_wait_mask <= wait_mask;
            spi_operation <= op;
            spi_start <= 1'b1;
            operation_started <= 1'b1;
            counter <= 32'd0;
        end
    endtask

    assign state = state_reg;
    assign busy = state_reg != ST_ARM_SAFE && state_reg != ST_SHUTDOWN_SAFE &&
                  state_reg != ST_FAULT_SAFE;
    assign done = data_window_passed;
    assign shutdown_safe = state_reg == ST_SHUTDOWN_SAFE;
    assign fault_safe = state_reg == ST_FAULT_SAFE;
    assign synchronized_inputs = {input_sync_3[1], input_sync_2[1],
                                  input_sync_1[1], input_sync_0[1]};

    assign ads1_consistent = ads1_read_valid == 3'b111 &&
        ads1_id_reads[7:0] == ads1_id_reads[15:8] &&
        ads1_id_reads[7:0] == ads1_id_reads[23:16];
    assign ads2_consistent = ads2_read_valid == 3'b111 &&
        ads2_id_reads[7:0] == ads2_id_reads[15:8] &&
        ads2_id_reads[7:0] == ads2_id_reads[23:16];
    assign ads1_all_expected = ads1_read_valid == 3'b111 &&
        ((ads1_id_reads[7:0] & ADS1299_ID_MASK) == ADS1299_ID_EXPECTED) &&
        ((ads1_id_reads[15:8] & ADS1299_ID_MASK) == ADS1299_ID_EXPECTED) &&
        ((ads1_id_reads[23:16] & ADS1299_ID_MASK) == ADS1299_ID_EXPECTED);
    assign ads2_all_expected = ads2_read_valid == 3'b111 &&
        ((ads2_id_reads[7:0] & ADS1299_ID_MASK) == ADS1299_ID_EXPECTED) &&
        ((ads2_id_reads[15:8] & ADS1299_ID_MASK) == ADS1299_ID_EXPECTED) &&
        ((ads2_id_reads[23:16] & ADS1299_ID_MASK) == ADS1299_ID_EXPECTED);
    assign id_gate_passed = ads1_consistent && ads2_consistent &&
                            ads1_all_expected && ads2_all_expected;
    assign id_valid = {&ads2_read_valid, &ads1_read_valid};
    assign id_diagnostic_flags = {
        2'b00, !(&ads2_read_valid), !(&ads1_read_valid),
        (&ads2_read_valid) && !ads2_all_expected,
        (&ads1_read_valid) && !ads1_all_expected,
        (&ads2_read_valid) && !ads2_consistent,
        (&ads1_read_valid) && !ads1_consistent
    };
    assign id_summary = {ads2_all_expected, ads1_all_expected,
                         ads2_consistent, ads1_consistent};
    assign config_passed = ads1_config_pass_bitmap == 11'h7FF &&
                           ads2_config_pass_bitmap == 11'h7FF;
    assign config_status = {config_passed,
                            ads2_config_pass_bitmap == 11'h7FF,
                            ads1_config_pass_bitmap == 11'h7FF,
                            state_reg == ST_CONFIG_WREG || state_reg == ST_CONFIG_RREG};
    assign data_window_active = state_reg == ST_WAIT_DRDY ||
                                state_reg == ST_FRAME_RECEIVE ||
                                state_reg == ST_TEST_DATA_WINDOW;
    assign data_window_summary = {
        1'b0,
        complete_pair_count >= WINDOW_VALID_FRAMES,
        frame_timeout_count == 0,
        partial_frame_count == 0,
        status_header_error_count == 0,
        valid_channel_positions == 16'hFFFF,
        data_window_passed,
        data_window_active
    };

    assign error_flags = full_error_flags[7:0];
    assign transport_error_flags = {4'b0000,
        |(full_error_flags & ERR_ILLEGAL_CS),
        |(full_error_flags & ERR_SPI_PROTOCOL),
        |(full_error_flags & ERR_SPI_TIMEOUT),
        1'b0};
    assign register_error_flags = {7'b0000000,
        |(full_error_flags & ERR_CONFIG_READBACK)};
    assign test_error_flags = {
        |(full_error_flags & ERR_INVALID_STATE),
        |(full_error_flags & ERR_WATCHDOG),
        |(full_error_flags & ERR_DATA_WINDOW),
        |(full_error_flags & ERR_STATUS_HEADER),
        |(full_error_flags & ERR_FRAME_LENGTH),
        |(full_error_flags & ERR_PARTIAL_FRAME),
        |(full_error_flags & ERR_DRDY_PAIR),
        |(full_error_flags & ERR_ID_GATE)
    };

    assign cmd_selected = state_reg == ST_ADS1_SDATAC ||
        (state_reg >= ST_ADS1_ID_READ_1 && state_reg <= ST_ADS2_ID_READ_3) ||
        state_reg == ST_ADS2_SDATAC || state_reg == ST_CONFIG_WREG ||
        state_reg == ST_CONFIG_RREG || state_reg == ST_RDATAC_ADS1 ||
        state_reg == ST_RDATAC_ADS2 || state_reg == ST_EXIT_SDATAC_ADS1 ||
        state_reg == ST_EXIT_SDATAC_ADS2;
    assign data_selected = data_window_active;
    assign frame_enable = data_selected && !shutdown_pending && !fault_pending;
    assign safe_boundary = !cmd_busy && !frame_rx_busy;
    assign watchdog_expired = WATCHDOG_CYCLES != 0 && watchdog_counter >= WATCHDOG_CYCLES - 1;
    assign shutdown_requested = shutdown_pending || software_shutdown_req;

    assign adc_pwdn_n = busy;
    assign adc_reset_n = busy && state_reg != ST_RESET_PULSE_LOW;
    assign adc_start = busy && state_reg >= ST_START_ASSERT &&
                       state_reg <= ST_TEST_DATA_WINDOW;
    assign adc_sclk = cmd_selected ? cmd_sclk : (data_selected ? frame_sclk : 1'b0);
    assign adc_din = cmd_selected ? cmd_din : (data_selected ? frame_din : 1'b0);
    // CS_N is active low: terminal states must leave both external devices
    // deselected.  The frozen seven-bit diagnostic bus reports CS activity
    // (active-high) so its safe value remains 0000000 without asserting pins.
    assign adc1_cs_n = busy ? (cmd_selected ? cmd_adc1_cs_n :
                       (data_selected ? frame_adc1_cs_n : 1'b1)) : 1'b1;
    assign adc2_cs_n = busy ? (cmd_selected ? cmd_adc2_cs_n :
                       (data_selected ? frame_adc2_cs_n : 1'b1)) : 1'b1;
    assign control_outputs = {adc_din, adc_pwdn_n, adc_reset_n, adc_sclk,
                              adc_start, ~adc1_cs_n, ~adc2_cs_n};
    assign spi_busy = cmd_busy;
    assign spi_done = cmd_done;

    ads1299_spi_phy_4d #(
        .SYS_CLK_HZ(SYS_CLK_HZ), .ADS_CLK_HZ(ADS_CLK_HZ),
        .SCLK_HALF_DIV(SCLK_HALF_DIV), .MAX_BITS(216)
    ) command_phy (
        .clk(clk), .rst_n(rst_n), .start(spi_start),
        .adc_mask(spi_adc_mask), .transfer_bits(spi_transfer_bits),
        .tx_bits(spi_tx_bits), .interbyte_wait_mask(spi_interbyte_wait_mask),
        .adc1_dout(adc1_dout), .adc2_dout(adc2_dout),
        .busy(cmd_busy), .done(cmd_done), .adc_sclk(cmd_sclk),
        .adc_din(cmd_din), .adc1_cs_n(cmd_adc1_cs_n),
        .adc2_cs_n(cmd_adc2_cs_n), .adc1_rx_bits(cmd_adc1_rx_bits),
        .adc2_rx_bits(cmd_adc2_rx_bits)
    );

    ads1299_dual_frame_rx_production #(
        .SYS_CLK_HZ(SYS_CLK_HZ), .ADS_CLK_HZ(ADS_CLK_HZ),
        .SCLK_HALF_DIV(SCLK_HALF_DIV),
        .PARTIAL_TIMEOUT_CYCLES(PARTIAL_TIMEOUT_CYCLES)
    ) frame_receiver (
        .clk(clk), .rst_n(rst_n), .enable(frame_enable),
        .adc1_drdy_n(adc1_drdy_n), .adc2_drdy_n(adc2_drdy_n),
        .adc1_dout(adc1_dout), .adc2_dout(adc2_dout),
        .adc_sclk(frame_sclk), .adc_din(frame_din),
        .adc1_cs_n(frame_adc1_cs_n), .adc2_cs_n(frame_adc2_cs_n),
        .busy(frame_rx_busy), .frame_valid(frame_valid),
        .partial_frame_event(partial_frame_event),
        .drdy_pair_event(drdy_pair_event), .adc1_frame(rx_adc1_frame),
        .adc2_frame(rx_adc2_frame), .total_drdy_events(total_drdy_event_count),
        .partial_frame_count(rx_partial_count), .desync_count(rx_desync_count),
        .timeout_count(rx_timeout_count), .received_bits(frame_received_bits),
        .frame_boundary_safe(frame_boundary_safe), .drdy_pair_state(drdy_pair_state)
    );
    assign partial_frame_count = rx_partial_count;
    assign drdy_error_count = rx_desync_count;
    assign frame_timeout_count = rx_timeout_count + data_wait_timeout_count;
    // Compatibility extension for the unified 4D-4I path.  Only the eight
    // post-settling, length/header-valid pairs are admitted to the 4F FIFO.
    assign production_pair_valid = frame_valid && id_gate_passed && config_passed &&
        discarded_settling_count >= 3 && frame_received_bits == 9'd216 &&
        rx_adc1_frame[215:212] == 4'hC && rx_adc2_frame[215:212] == 4'hC;
    assign production_pair_sequence = complete_pair_count;
    assign production_ads1_frame = rx_adc1_frame;
    assign production_ads2_frame = rx_adc2_frame;
    assign production_partial_event = partial_frame_event;
    assign production_partial_mask = drdy_pair_state;

    always @* begin
        state_next = state_reg;
        transition_error = 16'd0;
        if (cmd_selected && operation_started && !spi_timeout_latched &&
            counter >= SPI_TIMEOUT_CYCLES - 1 && !cmd_done) begin
            // Latch the fault but let the deterministic PHY finish.  The
            // fault_pending path enters the terminal state at the boundary.
            state_next = state_reg;
            transition_error = ERR_SPI_TIMEOUT;
        end else if (watchdog_expired && !watchdog_latched && busy &&
                     state_reg != ST_EXIT_STOP &&
                     state_reg != ST_EXIT_SDATAC_ADS1 &&
                     state_reg != ST_EXIT_SDATAC_ADS2) begin
            state_next = data_window_active && safe_boundary ? ST_EXIT_STOP :
                         (safe_boundary ? ST_FAULT_SAFE : state_reg);
            transition_error = ERR_WATCHDOG;
        end else if (fault_pending && safe_boundary && state_reg != ST_FAULT_SAFE &&
            state_reg != ST_SHUTDOWN_SAFE) begin
            state_next = data_window_active ? ST_EXIT_STOP : ST_FAULT_SAFE;
        end else if (shutdown_requested && safe_boundary &&
                     state_reg != ST_ARM_SAFE && state_reg != ST_EXIT_STOP &&
                     state_reg != ST_EXIT_SDATAC_ADS1 &&
                     state_reg != ST_EXIT_SDATAC_ADS2 &&
                     state_reg != ST_SHUTDOWN_SAFE && state_reg != ST_FAULT_SAFE) begin
            state_next = ST_EXIT_STOP;
        end else begin
            case (state_reg)
                ST_ARM_SAFE: if (arm_start_req) state_next = ST_STARTUP_TPOR_WAIT;
                ST_STARTUP_TPOR_WAIT:
                    if (counter >= TPOR_WAIT_CYCLES - 1) state_next = ST_VCAP_SETTLE_WAIT;
                ST_VCAP_SETTLE_WAIT:
                    if (counter >= VCAP_SETTLE_WAIT_CYCLES - 1) state_next = ST_RESET_PULSE_LOW;
                ST_RESET_PULSE_LOW:
                    if (counter >= RESET_LOW_CYCLES - 1) state_next = ST_RESET_RELEASE_WAIT;
                ST_RESET_RELEASE_WAIT:
                    if (counter >= RESET_RELEASE_WAIT_CYCLES - 1) state_next = ST_SPI_PREPARE;
                ST_SPI_PREPARE:
                    if (counter >= SPI_PREPARE_CYCLES - 1) state_next = ST_ADS1_SDATAC;
                ST_ADS1_SDATAC:
                    if (operation_started && cmd_done) state_next = ST_ADS1_ID_READ_1;
                ST_ADS1_ID_READ_1:
                    if (operation_started && cmd_done) state_next = ST_ADS1_ID_READ_2;
                ST_ADS1_ID_READ_2:
                    if (operation_started && cmd_done) state_next = ST_ADS1_ID_READ_3;
                ST_ADS1_ID_READ_3:
                    if (operation_started && cmd_done) state_next = ST_ADS2_SDATAC;
                ST_ADS2_SDATAC:
                    if (operation_started && cmd_done) state_next = ST_ADS2_ID_READ_1;
                ST_ADS2_ID_READ_1:
                    if (operation_started && cmd_done) state_next = ST_ADS2_ID_READ_2;
                ST_ADS2_ID_READ_2:
                    if (operation_started && cmd_done) state_next = ST_ADS2_ID_READ_3;
                ST_ADS2_ID_READ_3:
                    if (operation_started && cmd_done) state_next = ST_ID_CONFIRM;
                ST_ID_CONFIRM: begin
                    if (!id_gate_passed) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERR_ID_GATE;
                    end else state_next = ST_CONFIG_WREG;
                end
                ST_CONFIG_WREG:
                    if (operation_started && cmd_done) state_next = ST_CONFIG_RREG;
                ST_CONFIG_RREG: begin
                    if (operation_started && cmd_done) begin
                        if (((current_config_device ? cmd_adc2_rx_bits[7:0] :
                              cmd_adc1_rx_bits[7:0]) & config_mask(current_config_index)) !=
                            (config_value(current_config_index) & config_mask(current_config_index))) begin
                            state_next = ST_FAULT_SAFE;
                            transition_error = ERR_CONFIG_READBACK;
                        end else if (current_config_index == 4'd10 && current_config_device) begin
                            state_next = ST_RDATAC_ADS1;
                        end else begin
                            state_next = ST_CONFIG_WREG;
                        end
                    end
                end
                ST_RDATAC_ADS1:
                    if (operation_started && cmd_done) state_next = ST_RDATAC_ADS2;
                ST_RDATAC_ADS2:
                    if (operation_started && cmd_done) state_next = ST_START_ASSERT;
                ST_START_ASSERT:
                    if (counter >= START_ASSERT_CYCLES - 1) state_next = ST_WAIT_DRDY;
                ST_WAIT_DRDY: begin
                    if (frame_rx_busy) state_next = ST_FRAME_RECEIVE;
                    else if (partial_frame_event) begin
                        state_next = ST_EXIT_STOP;
                        transition_error = ERR_DRDY_PAIR | ERR_PARTIAL_FRAME;
                    end else if (counter >= DATA_FRAME_TIMEOUT_CYCLES - 1) begin
                        state_next = ST_EXIT_STOP;
                        transition_error = ERR_DATA_WINDOW;
                    end
                end
                ST_FRAME_RECEIVE: begin
                    if (partial_frame_event) begin
                        state_next = ST_EXIT_STOP;
                        transition_error = ERR_DRDY_PAIR | ERR_PARTIAL_FRAME;
                    end else if (frame_valid) begin
                        if (frame_received_bits != 9'd216) begin
                            state_next = ST_EXIT_STOP;
                            transition_error = ERR_FRAME_LENGTH;
                        end else if (rx_adc1_frame[215:212] != 4'hC ||
                                     rx_adc2_frame[215:212] != 4'hC) begin
                            state_next = ST_EXIT_STOP;
                            transition_error = ERR_STATUS_HEADER;
                        end else if (discarded_settling_count >= 3 &&
                                     complete_pair_count >= WINDOW_VALID_FRAMES - 1) begin
                            state_next = ST_EXIT_STOP;
                        end else state_next = ST_TEST_DATA_WINDOW;
                    end
                end
                ST_TEST_DATA_WINDOW: begin
                    if (test_data_window_error_in) begin
                        state_next = ST_EXIT_STOP;
                        transition_error = ERR_DATA_WINDOW;
                    end else if (frame_rx_busy) state_next = ST_FRAME_RECEIVE;
                    else state_next = ST_WAIT_DRDY;
                end
                ST_EXIT_STOP: state_next = ST_EXIT_SDATAC_ADS1;
                ST_EXIT_SDATAC_ADS1:
                    if (operation_started && cmd_done) state_next = ST_EXIT_SDATAC_ADS2;
                ST_EXIT_SDATAC_ADS2:
                    if (operation_started && cmd_done)
                        state_next = exit_to_fault ? ST_FAULT_SAFE : ST_SHUTDOWN_SAFE;
                ST_SHUTDOWN_SAFE: state_next = ST_SHUTDOWN_SAFE;
                ST_FAULT_SAFE: state_next = ST_FAULT_SAFE;
                default: begin
                    state_next = ST_FAULT_SAFE;
                    transition_error = ERR_INVALID_STATE;
                end
            endcase
        end

    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state_reg <= ST_ARM_SAFE;
            counter <= 32'd0;
            watchdog_counter <= 32'd0;
            accepted_event_count <= 2'd0;
            operation_started <= 1'b0;
            spi_adc_mask <= 2'b00;
            spi_tx_bits <= 216'd0;
            spi_interbyte_wait_mask <= 32'd0;
            spi_transfer_bits <= 9'd0;
            spi_start <= 1'b0;
            spi_operation <= SPI_OP_IDLE;
            ads1_id_raw <= 8'd0;
            ads2_id_raw <= 8'd0;
            ads1_id_reads <= 24'd0;
            ads2_id_reads <= 24'd0;
            ads1_read_valid <= 3'b000;
            ads2_read_valid <= 3'b000;
            ads1_config_readbacks <= 88'd0;
            ads2_config_readbacks <= 88'd0;
            ads1_config_pass_bitmap <= 11'd0;
            ads2_config_pass_bitmap <= 11'd0;
            current_config_address <= 8'h01;
            current_config_index <= 4'd0;
            current_config_device <= 1'b0;
            complete_pair_count <= 32'd0;
            discarded_settling_count <= 32'd0;
            spi_error_count <= 32'd0;
            frame_length_error_count <= 32'd0;
            status_header_error_count <= 32'd0;
            last_ads1_status <= 24'd0;
            last_ads2_status <= 24'd0;
            last_ads1_channels <= 192'd0;
            last_ads2_channels <= 192'd0;
            valid_channel_positions <= 16'd0;
            data_window_passed <= 1'b0;
            full_error_flags <= 16'd0;
            shutdown_reason <= REASON_NONE;
            shutdown_req_seen <= 1'b0;
            shutdown_pending <= 1'b0;
            fault_pending <= 1'b0;
            exit_to_fault <= 1'b0;
            spi_timeout_latched <= 1'b0;
            watchdog_latched <= 1'b0;
            data_wait_timeout_count <= 32'd0;
            input_sync_0 <= 2'b11;
            input_sync_1 <= 2'b11;
            input_sync_2 <= 2'b00;
            input_sync_3 <= 2'b00;
        end else begin
            state_reg <= state_next;
            spi_start <= 1'b0;
            input_sync_0 <= {input_sync_0[0], adc1_drdy_n};
            input_sync_1 <= {input_sync_1[0], adc2_drdy_n};
            input_sync_2 <= {input_sync_2[0], adc1_dout};
            input_sync_3 <= {input_sync_3[0], adc2_dout};

            if (clear_sticky_req && state_reg == ST_ARM_SAFE) begin
                full_error_flags <= 16'd0;
                shutdown_reason <= REASON_NONE;
            end

            if (state_next != state_reg) begin
                counter <= 32'd0;
                operation_started <= 1'b0;
                spi_operation <= SPI_OP_IDLE;
            end else begin
                counter <= counter + 32'd1;
            end

            if (state_reg == ST_ARM_SAFE || state_reg == ST_SHUTDOWN_SAFE ||
                state_reg == ST_FAULT_SAFE)
                watchdog_counter <= 32'd0;
            else if (!watchdog_expired)
                watchdog_counter <= watchdog_counter + 32'd1;

            if (arm_start_req && state_reg == ST_ARM_SAFE)
                accepted_event_count <= 2'd1;

            if (software_shutdown_req) begin
                shutdown_pending <= 1'b1;
                shutdown_req_seen <= 1'b1;
                if (shutdown_reason == REASON_NONE) shutdown_reason <= REASON_SOFTWARE;
            end

            if (protocol_fault_in || illegal_cs_fault_in) begin
                fault_pending <= 1'b1;
                exit_to_fault <= 1'b1;
                full_error_flags <= full_error_flags |
                    (protocol_fault_in ? ERR_SPI_PROTOCOL : 16'd0) |
                    (illegal_cs_fault_in ? ERR_ILLEGAL_CS : 16'd0);
                spi_error_count <= spi_error_count + 32'd1;
                shutdown_reason <= REASON_FAULT;
            end

            if (transition_error != 0) begin
                full_error_flags <= full_error_flags | transition_error;
                if (transition_error & (ERR_DRDY_PAIR | ERR_PARTIAL_FRAME |
                    ERR_FRAME_LENGTH | ERR_STATUS_HEADER | ERR_DATA_WINDOW |
                    ERR_WATCHDOG | ERR_SPI_TIMEOUT)) begin
                    exit_to_fault <= 1'b1;
                    fault_pending <= 1'b1;
                end
                if (transition_error & ERR_SPI_TIMEOUT)
                    spi_timeout_latched <= 1'b1;
                if (transition_error & ERR_WATCHDOG)
                    watchdog_latched <= 1'b1;
                if (transition_error & ERR_DATA_WINDOW)
                    data_wait_timeout_count <= data_wait_timeout_count + 32'd1;
                if (transition_error & (ERR_SPI_TIMEOUT | ERR_SPI_PROTOCOL |
                    ERR_ILLEGAL_CS)) spi_error_count <= spi_error_count + 32'd1;
                if (transition_error & ERR_FRAME_LENGTH)
                    frame_length_error_count <= frame_length_error_count + 32'd1;
                if (transition_error & ERR_STATUS_HEADER)
                    status_header_error_count <= status_header_error_count + 32'd1;
                shutdown_reason <= (transition_error & ERR_WATCHDOG) ?
                    REASON_WATCHDOG : REASON_FAULT;
            end

            if (watchdog_expired) shutdown_pending <= 1'b1;

            case (state_reg)
                ST_ARM_SAFE: begin
                    operation_started <= 1'b0;
                    if (arm_start_req) begin
                        ads1_id_reads <= 24'd0;
                        ads2_id_reads <= 24'd0;
                        ads1_read_valid <= 3'b000;
                        ads2_read_valid <= 3'b000;
                        ads1_config_readbacks <= 88'd0;
                        ads2_config_readbacks <= 88'd0;
                        ads1_config_pass_bitmap <= 11'd0;
                        ads2_config_pass_bitmap <= 11'd0;
                        current_config_index <= 4'd0;
                        current_config_device <= 1'b0;
                        current_config_address <= 8'h01;
                        complete_pair_count <= 32'd0;
                        discarded_settling_count <= 32'd0;
                        valid_channel_positions <= 16'd0;
                        data_window_passed <= 1'b0;
                        shutdown_pending <= 1'b0;
                        shutdown_req_seen <= 1'b0;
                        fault_pending <= 1'b0;
                        exit_to_fault <= 1'b0;
                        spi_timeout_latched <= 1'b0;
                        watchdog_latched <= 1'b0;
                        data_wait_timeout_count <= 32'd0;
                    end
                end
                ST_ADS1_SDATAC:
                    if (!operation_started) launch_spi(2'b01, 9'd8, 24'h000011, 0, SPI_OP_SDATAC);
                ST_ADS2_SDATAC:
                    if (!operation_started) launch_spi(2'b10, 9'd8, 24'h000011, 0, SPI_OP_SDATAC);
                ST_ADS1_ID_READ_1, ST_ADS1_ID_READ_2, ST_ADS1_ID_READ_3: begin
                    if (!operation_started)
                        launch_spi(2'b01, 9'd24, 24'h20_00_00, 32'h3, SPI_OP_ID_RREG);
                    else if (cmd_done) begin
                        ads1_id_raw <= cmd_adc1_rx_bits[7:0];
                        if (state_reg == ST_ADS1_ID_READ_1) begin
                            ads1_id_reads[7:0] <= cmd_adc1_rx_bits[7:0]; ads1_read_valid[0] <= 1'b1;
                        end else if (state_reg == ST_ADS1_ID_READ_2) begin
                            ads1_id_reads[15:8] <= cmd_adc1_rx_bits[7:0]; ads1_read_valid[1] <= 1'b1;
                        end else begin
                            ads1_id_reads[23:16] <= cmd_adc1_rx_bits[7:0]; ads1_read_valid[2] <= 1'b1;
                        end
                    end
                end
                ST_ADS2_ID_READ_1, ST_ADS2_ID_READ_2, ST_ADS2_ID_READ_3: begin
                    if (!operation_started)
                        launch_spi(2'b10, 9'd24, 24'h20_00_00, 32'h3, SPI_OP_ID_RREG);
                    else if (cmd_done) begin
                        ads2_id_raw <= cmd_adc2_rx_bits[7:0];
                        if (state_reg == ST_ADS2_ID_READ_1) begin
                            ads2_id_reads[7:0] <= cmd_adc2_rx_bits[7:0]; ads2_read_valid[0] <= 1'b1;
                        end else if (state_reg == ST_ADS2_ID_READ_2) begin
                            ads2_id_reads[15:8] <= cmd_adc2_rx_bits[7:0]; ads2_read_valid[1] <= 1'b1;
                        end else begin
                            ads2_id_reads[23:16] <= cmd_adc2_rx_bits[7:0]; ads2_read_valid[2] <= 1'b1;
                        end
                    end
                end
                ST_ID_CONFIRM: begin
                    current_config_index <= 4'd0;
                    current_config_device <= 1'b0;
                    current_config_address <= 8'h01;
                end
                ST_CONFIG_WREG: begin
                    current_config_address <= config_address(current_config_index);
                    if (!operation_started)
                        launch_spi(current_config_device ? 2'b10 : 2'b01, 9'd24,
                            {8'h40 | config_address(current_config_index), 8'h00,
                             config_value(current_config_index)}, 32'h3, SPI_OP_CONFIG_WREG);
                end
                ST_CONFIG_RREG: begin
                    if (!operation_started)
                        launch_spi(current_config_device ? 2'b10 : 2'b01, 9'd24,
                            {8'h20 | config_address(current_config_index), 16'h0000},
                            32'h3, SPI_OP_CONFIG_RREG);
                    else if (cmd_done) begin
                        if (current_config_device) begin
                            ads2_config_readbacks[current_config_index*8 +: 8] <= cmd_adc2_rx_bits[7:0];
                            if ((cmd_adc2_rx_bits[7:0] & config_mask(current_config_index)) ==
                                (config_value(current_config_index) & config_mask(current_config_index)))
                                ads2_config_pass_bitmap[current_config_index] <= 1'b1;
                        end else begin
                            ads1_config_readbacks[current_config_index*8 +: 8] <= cmd_adc1_rx_bits[7:0];
                            if ((cmd_adc1_rx_bits[7:0] & config_mask(current_config_index)) ==
                                (config_value(current_config_index) & config_mask(current_config_index)))
                                ads1_config_pass_bitmap[current_config_index] <= 1'b1;
                        end
                        if (current_config_index == 4'd10) begin
                            if (!current_config_device) begin
                                current_config_device <= 1'b1;
                                current_config_index <= 4'd0;
                                current_config_address <= 8'h01;
                            end
                        end else begin
                            current_config_index <= current_config_index + 4'd1;
                            current_config_address <= config_address(current_config_index + 4'd1);
                        end
                    end
                end
                ST_RDATAC_ADS1:
                    if (!operation_started) launch_spi(2'b01, 9'd8, 24'h000010, 0, SPI_OP_RDATAC);
                ST_RDATAC_ADS2:
                    if (!operation_started) launch_spi(2'b10, 9'd8, 24'h000010, 0, SPI_OP_RDATAC);
                ST_FRAME_RECEIVE: begin
                    if (frame_valid) begin
                        last_ads1_status <= rx_adc1_frame[215:192];
                        last_ads2_status <= rx_adc2_frame[215:192];
                        last_ads1_channels <= rx_adc1_frame[191:0];
                        last_ads2_channels <= rx_adc2_frame[191:0];
                        if (frame_received_bits == 9'd216 &&
                            rx_adc1_frame[215:212] == 4'hC &&
                            rx_adc2_frame[215:212] == 4'hC) begin
                            valid_channel_positions <= 16'hFFFF;
                            if (discarded_settling_count < 3)
                                discarded_settling_count <= discarded_settling_count + 32'd1;
                            else begin
                                complete_pair_count <= complete_pair_count + 32'd1;
                                if (complete_pair_count >= WINDOW_VALID_FRAMES - 1) begin
                                    data_window_passed <= 1'b1;
                                    shutdown_pending <= 1'b1;
                                    shutdown_reason <= REASON_TEST_COMPLETE;
                                end
                            end
                        end
                    end
                end
                ST_EXIT_STOP: operation_started <= 1'b0;
                ST_EXIT_SDATAC_ADS1:
                    if (!operation_started)
                        launch_spi(2'b01, 9'd8, 24'h000011, 0, SPI_OP_EXIT_SDATAC);
                ST_EXIT_SDATAC_ADS2:
                    if (!operation_started)
                        launch_spi(2'b10, 9'd8, 24'h000011, 0, SPI_OP_EXIT_SDATAC);
                ST_SHUTDOWN_SAFE, ST_FAULT_SAFE: begin
                    operation_started <= 1'b0;
                    shutdown_pending <= 1'b1;
                end
                default: ;
            endcase
        end
    end
endmodule
