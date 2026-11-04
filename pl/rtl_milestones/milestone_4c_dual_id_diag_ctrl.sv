`timescale 1ns/1ps

// Milestone 4C-DUAL-ID-DIAG controller.
// One qualified key event executes D0, reads each ADS1299 ID three times,
// records ID mismatches as diagnostic results, then shuts down safely.
// Transport/protocol failures remain fatal and enter locked FAULT_SAFE.
module milestone_4c_dual_id_diag_ctrl #(
    parameter logic [31:0] KEY_DEBOUNCE_CYCLES       = 32'd1_000_000,
    parameter logic [31:0] TPOR_WAIT_CYCLES          = 32'd21_000_000,
    parameter logic [31:0] VCAP_SETTLE_WAIT_CYCLES   = 32'd100_000_000,
    parameter logic [31:0] RESET_LOW_CYCLES          = 32'd100_000,
    parameter logic [31:0] RESET_RELEASE_WAIT_CYCLES = 32'd100_000,
    parameter logic [31:0] SPI_PREPARE_CYCLES        = 32'd196,
    parameter logic [31:0] SPI_TIMEOUT_CYCLES        = 32'd100_000,
    parameter logic [31:0] RESULT_HOLD_CYCLES        = 32'd500_000_000,
    parameter integer      SYS_CLK_HZ                = 100_000_000,
    parameter integer      ADS_CLK_HZ                = 2_048_000,
    parameter integer      SCLK_HALF_DIV             = 50
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        key_n,
    input  wire [3:0]  raw_inputs,
    output wire [4:0]  state,
    output reg  [31:0] counter,
    output reg  [7:0]  error_flags,
    output wire        done,
    output wire        shutdown_safe,
    output wire        fault_safe,
    output wire        key_synchronized,
    output reg         key_armed,
    output reg         key_press_event,
    output reg  [1:0]  accepted_event_count,
    output wire [3:0]  synchronized_inputs,
    output wire [6:0]  control_outputs,
    output wire        adc_din,
    output wire        adc_pwdn_n,
    output wire        adc_reset_n,
    output wire        adc_sclk,
    output wire        adc_start,
    output wire        adc1_cs_n,
    output wire        adc2_cs_n,
    output wire        spi_busy,
    output wire        spi_done,
    output reg         spi_start,
    output reg  [2:0]  spi_operation,
    output reg  [8:0]  spi_transfer_bits,
    output reg  [7:0]  ads1_id_raw,
    output reg  [7:0]  ads2_id_raw,
    output wire        ads1_id_valid,
    output wire        ads2_id_valid,
    output reg  [23:0] ads1_id_reads,
    output reg  [23:0] ads2_id_reads,
    output wire        ads1_reads_consistent,
    output wire        ads2_reads_consistent,
    output wire        ads1_all_expected,
    output wire        ads2_all_expected,
    output reg         ads1_any_transport_error,
    output reg         ads2_any_transport_error,
    output wire [2:0]  id_read_index,
    output wire [7:0]  id_diagnostic_flags
);

    localparam logic [4:0] ST_ARM_SAFE           = 5'd0;
    localparam logic [4:0] ST_STARTUP_TPOR_WAIT  = 5'd1;
    localparam logic [4:0] ST_VCAP_SETTLE_WAIT   = 5'd2;
    localparam logic [4:0] ST_RESET_PULSE_LOW    = 5'd3;
    localparam logic [4:0] ST_RESET_RELEASE_WAIT = 5'd4;
    localparam logic [4:0] ST_SPI_PREPARE        = 5'd5;
    localparam logic [4:0] ST_ADS1_SDATAC        = 5'd6;
    localparam logic [4:0] ST_ADS1_ID_READ_1     = 5'd7;
    localparam logic [4:0] ST_ADS1_ID_READ_2     = 5'd8;
    localparam logic [4:0] ST_ADS1_ID_READ_3     = 5'd9;
    localparam logic [4:0] ST_ADS2_SDATAC        = 5'd10;
    localparam logic [4:0] ST_ADS2_ID_READ_1     = 5'd11;
    localparam logic [4:0] ST_ADS2_ID_READ_2     = 5'd12;
    localparam logic [4:0] ST_ADS2_ID_READ_3     = 5'd13;
    localparam logic [4:0] ST_ID_RESULT_HOLD     = 5'd14;
    localparam logic [4:0] ST_SHUTDOWN_SAFE      = 5'd15;
    localparam logic [4:0] ST_FAULT_SAFE         = 5'd16;

    localparam logic [2:0] SPI_OP_IDLE        = 3'd0;
    localparam logic [2:0] SPI_OP_ADS1_SDATAC = 3'd1;
    localparam logic [2:0] SPI_OP_ADS1_RREG   = 3'd2;
    localparam logic [2:0] SPI_OP_ADS2_SDATAC = 3'd3;
    localparam logic [2:0] SPI_OP_ADS2_RREG   = 3'd4;

    localparam logic [7:0] ERROR_INVALID_STATE = 8'h01;
    localparam logic [7:0] ERROR_SPI_TIMEOUT   = 8'h02;
    localparam logic [7:0] ERROR_DUAL_CS       = 8'h04;
    localparam logic [7:0] ERROR_INCOMPLETE_ID = 8'h20;
    localparam logic [7:0] ERROR_INTERNAL      = 8'h40;

    // Frozen 8-channel ADS1299 criterion. Diagnostic mode never relaxes it.
    localparam logic [7:0] ADS1299_ID_MASK     = 8'h1F;
    localparam logic [7:0] ADS1299_ID_EXPECTED = 8'h1E;

    (* fsm_encoding = "user_encoding", fsm_safe_state = "default_state" *)
    reg [4:0] state_reg = ST_ARM_SAFE;
    reg [4:0] state_next;
    reg [7:0] transition_error;

    (* ASYNC_REG = "TRUE" *) reg [1:0] input_sync_0 = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] input_sync_1 = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] input_sync_2 = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] input_sync_3 = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] key_sync_ff  = 2'b00;

    reg [31:0] key_debounce_counter = 32'd0;
    reg operation_started = 1'b0;
    reg [1:0] spi_adc_mask = 2'b00;
    reg [23:0] spi_tx_bits = 24'd0;
    reg [31:0] spi_interbyte_wait_mask = 32'd0;
    reg [2:0] ads1_read_valid = 3'b000;
    reg [2:0] ads2_read_valid = 3'b000;

    wire spi_adc_sclk;
    wire spi_adc_din;
    wire spi_adc1_cs_n;
    wire spi_adc2_cs_n;
    wire [23:0] spi_adc1_rx_bits;
    wire [23:0] spi_adc2_rx_bits;

    wire [7:0] ads1_read_1 = ads1_id_reads[7:0];
    wire [7:0] ads1_read_2 = ads1_id_reads[15:8];
    wire [7:0] ads1_read_3 = ads1_id_reads[23:16];
    wire [7:0] ads2_read_1 = ads2_id_reads[7:0];
    wire [7:0] ads2_read_2 = ads2_id_reads[15:8];
    wire [7:0] ads2_read_3 = ads2_id_reads[23:16];

    wire ads1_any_id_mismatch =
        (ads1_read_valid[0] && ((ads1_read_1 & ADS1299_ID_MASK) != ADS1299_ID_EXPECTED)) ||
        (ads1_read_valid[1] && ((ads1_read_2 & ADS1299_ID_MASK) != ADS1299_ID_EXPECTED)) ||
        (ads1_read_valid[2] && ((ads1_read_3 & ADS1299_ID_MASK) != ADS1299_ID_EXPECTED));
    wire ads2_any_id_mismatch =
        (ads2_read_valid[0] && ((ads2_read_1 & ADS1299_ID_MASK) != ADS1299_ID_EXPECTED)) ||
        (ads2_read_valid[1] && ((ads2_read_2 & ADS1299_ID_MASK) != ADS1299_ID_EXPECTED)) ||
        (ads2_read_valid[2] && ((ads2_read_3 & ADS1299_ID_MASK) != ADS1299_ID_EXPECTED));

    assign ads1_id_valid = &ads1_read_valid;
    assign ads2_id_valid = &ads2_read_valid;
    assign ads1_reads_consistent = ads1_id_valid &&
        (ads1_read_1 == ads1_read_2) && (ads1_read_2 == ads1_read_3);
    assign ads2_reads_consistent = ads2_id_valid &&
        (ads2_read_1 == ads2_read_2) && (ads2_read_2 == ads2_read_3);
    assign ads1_all_expected = ads1_id_valid && !ads1_any_id_mismatch;
    assign ads2_all_expected = ads2_id_valid && !ads2_any_id_mismatch;
    assign id_diagnostic_flags = {
        2'b00,
        !ads2_id_valid,
        !ads1_id_valid,
        ads2_any_id_mismatch,
        ads1_any_id_mismatch,
        ads2_id_valid && !ads2_reads_consistent,
        ads1_id_valid && !ads1_reads_consistent
    };

    assign id_read_index =
        ((state_reg == ST_ADS1_ID_READ_1) || (state_reg == ST_ADS2_ID_READ_1)) ? 3'd1 :
        ((state_reg == ST_ADS1_ID_READ_2) || (state_reg == ST_ADS2_ID_READ_2)) ? 3'd2 :
        ((state_reg == ST_ADS1_ID_READ_3) || (state_reg == ST_ADS2_ID_READ_3)) ? 3'd3 : 3'd0;

    wire state_enables_ads =
        (state_reg == ST_STARTUP_TPOR_WAIT)  ||
        (state_reg == ST_VCAP_SETTLE_WAIT)   ||
        (state_reg == ST_RESET_PULSE_LOW)    ||
        (state_reg == ST_RESET_RELEASE_WAIT) ||
        (state_reg == ST_SPI_PREPARE)        ||
        (state_reg == ST_ADS1_SDATAC)        ||
        (state_reg == ST_ADS1_ID_READ_1)     ||
        (state_reg == ST_ADS1_ID_READ_2)     ||
        (state_reg == ST_ADS1_ID_READ_3)     ||
        (state_reg == ST_ADS2_SDATAC)        ||
        (state_reg == ST_ADS2_ID_READ_1)     ||
        (state_reg == ST_ADS2_ID_READ_2)     ||
        (state_reg == ST_ADS2_ID_READ_3)     ||
        (state_reg == ST_ID_RESULT_HOLD);

    wire state_enables_spi_outputs = state_enables_ads && rst_n;
    wire phy_cs_conflict = !spi_adc1_cs_n && !spi_adc2_cs_n;
    wire phy_rst_n = rst_n && state_enables_ads;
    wire state_is_ads1_spi =
        (state_reg == ST_ADS1_SDATAC) ||
        (state_reg == ST_ADS1_ID_READ_1) ||
        (state_reg == ST_ADS1_ID_READ_2) ||
        (state_reg == ST_ADS1_ID_READ_3);
    wire state_is_ads2_spi =
        (state_reg == ST_ADS2_SDATAC) ||
        (state_reg == ST_ADS2_ID_READ_1) ||
        (state_reg == ST_ADS2_ID_READ_2) ||
        (state_reg == ST_ADS2_ID_READ_3);

    initial begin
        counter                  = 32'd0;
        error_flags              = 8'd0;
        key_armed                = 1'b0;
        key_press_event          = 1'b0;
        accepted_event_count     = 2'd0;
        spi_start                = 1'b0;
        spi_operation            = SPI_OP_IDLE;
        spi_transfer_bits        = 9'd0;
        ads1_id_raw              = 8'd0;
        ads2_id_raw              = 8'd0;
        ads1_id_reads            = 24'd0;
        ads2_id_reads            = 24'd0;
        ads1_any_transport_error = 1'b0;
        ads2_any_transport_error = 1'b0;
    end

    assign state = state_reg;
    assign synchronized_inputs = {
        input_sync_3[1], input_sync_2[1], input_sync_1[1], input_sync_0[1]
    };
    assign key_synchronized = key_sync_ff[1];

    assign adc_pwdn_n = state_enables_ads;
    assign adc_reset_n =
        (state_reg == ST_STARTUP_TPOR_WAIT)  ||
        (state_reg == ST_VCAP_SETTLE_WAIT)   ||
        (state_reg == ST_RESET_RELEASE_WAIT) ||
        (state_reg == ST_SPI_PREPARE)        ||
        (state_reg == ST_ADS1_SDATAC)        ||
        (state_reg == ST_ADS1_ID_READ_1)     ||
        (state_reg == ST_ADS1_ID_READ_2)     ||
        (state_reg == ST_ADS1_ID_READ_3)     ||
        (state_reg == ST_ADS2_SDATAC)        ||
        (state_reg == ST_ADS2_ID_READ_1)     ||
        (state_reg == ST_ADS2_ID_READ_2)     ||
        (state_reg == ST_ADS2_ID_READ_3)     ||
        (state_reg == ST_ID_RESULT_HOLD);

    // Outside the active path all seven outputs fail low. During active idle,
    // SCLK/DIN are low and both CS_N are high. A dual-CS invariant breach is
    // externally suppressed and causes locked FAULT_SAFE.
    assign adc_din   = state_enables_spi_outputs ? spi_adc_din  : 1'b0;
    assign adc_sclk  = state_enables_spi_outputs ? spi_adc_sclk : 1'b0;
    assign adc_start = 1'b0;
    assign adc1_cs_n = state_enables_spi_outputs ?
                       (phy_cs_conflict ? 1'b1 : spi_adc1_cs_n) : 1'b0;
    assign adc2_cs_n = state_enables_spi_outputs ?
                       (phy_cs_conflict ? 1'b1 : spi_adc2_cs_n) : 1'b0;
    assign control_outputs = {
        adc_din, adc_pwdn_n, adc_reset_n, adc_sclk,
        adc_start, adc1_cs_n, adc2_cs_n
    };
    assign done          = (state_reg == ST_ID_RESULT_HOLD);
    assign shutdown_safe = (state_reg == ST_SHUTDOWN_SAFE);
    assign fault_safe    = (state_reg == ST_FAULT_SAFE);

    (* DONT_TOUCH = "TRUE" *) ads1299_spi_phy #(
        .SYS_CLK_HZ(SYS_CLK_HZ), .ADS_CLK_HZ(ADS_CLK_HZ),
        .SCLK_HALF_DIV(SCLK_HALF_DIV), .MAX_BITS(24)
    ) spi_phy (
        .clk(clk), .rst_n(phy_rst_n), .start(spi_start),
        .adc_mask(spi_adc_mask), .transfer_bits(spi_transfer_bits),
        .tx_bits(spi_tx_bits),
        .interbyte_wait_mask(spi_interbyte_wait_mask),
        .adc1_dout(raw_inputs[2]), .adc2_dout(raw_inputs[3]),
        .busy(spi_busy), .done(spi_done),
        .adc_sclk(spi_adc_sclk), .adc_din(spi_adc_din),
        .adc1_cs_n(spi_adc1_cs_n), .adc2_cs_n(spi_adc2_cs_n),
        .adc1_rx_bits(spi_adc1_rx_bits), .adc2_rx_bits(spi_adc2_rx_bits)
    );

    always @(posedge clk) begin
        if (!rst_n) begin
            input_sync_0         <= 2'b00;
            input_sync_1         <= 2'b00;
            input_sync_2         <= 2'b00;
            input_sync_3         <= 2'b00;
            key_sync_ff          <= 2'b00;
            key_debounce_counter <= 32'd0;
            key_armed            <= 1'b0;
            key_press_event      <= 1'b0;
        end else begin
            input_sync_0 <= {input_sync_0[0], raw_inputs[0]};
            input_sync_1 <= {input_sync_1[0], raw_inputs[1]};
            input_sync_2 <= {input_sync_2[0], raw_inputs[2]};
            input_sync_3 <= {input_sync_3[0], raw_inputs[3]};
            key_sync_ff  <= {key_sync_ff[0], key_n};
            key_press_event <= 1'b0;

            if (state_reg != ST_ARM_SAFE) begin
                key_debounce_counter <= 32'd0;
                key_armed            <= 1'b0;
            end else if (!key_armed) begin
                if (key_sync_ff[1]) begin
                    if (key_debounce_counter >= KEY_DEBOUNCE_CYCLES - 32'd1) begin
                        key_debounce_counter <= 32'd0;
                        key_armed            <= 1'b1;
                    end else key_debounce_counter <= key_debounce_counter + 32'd1;
                end else key_debounce_counter <= 32'd0;
            end else if (!key_sync_ff[1]) begin
                if (key_debounce_counter >= KEY_DEBOUNCE_CYCLES - 32'd1) begin
                    key_debounce_counter <= 32'd0;
                    key_armed            <= 1'b0;
                    key_press_event      <= 1'b1;
                end else key_debounce_counter <= key_debounce_counter + 32'd1;
            end else key_debounce_counter <= 32'd0;
        end
    end

    always @* begin
        state_next       = state_reg;
        transition_error = 8'd0;

        if ((state_reg != ST_ARM_SAFE) &&
            (state_reg != ST_SHUTDOWN_SAFE) &&
            (state_reg != ST_FAULT_SAFE) && phy_cs_conflict) begin
            state_next       = ST_FAULT_SAFE;
            transition_error = ERROR_DUAL_CS;
        end else begin
            case (state_reg)
                ST_ARM_SAFE:
                    if (key_press_event) state_next = ST_STARTUP_TPOR_WAIT;
                ST_STARTUP_TPOR_WAIT:
                    if (counter >= TPOR_WAIT_CYCLES - 32'd1) state_next = ST_VCAP_SETTLE_WAIT;
                ST_VCAP_SETTLE_WAIT:
                    if (counter >= VCAP_SETTLE_WAIT_CYCLES - 32'd1) state_next = ST_RESET_PULSE_LOW;
                ST_RESET_PULSE_LOW:
                    if (counter >= RESET_LOW_CYCLES - 32'd1) state_next = ST_RESET_RELEASE_WAIT;
                ST_RESET_RELEASE_WAIT:
                    if (counter >= RESET_RELEASE_WAIT_CYCLES - 32'd1) state_next = ST_SPI_PREPARE;
                ST_SPI_PREPARE: begin
                    if (spi_busy) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INTERNAL;
                    end else if (counter >= SPI_PREPARE_CYCLES - 32'd1)
                        state_next = ST_ADS1_SDATAC;
                end
                ST_ADS1_SDATAC: begin
                    if (!operation_started && spi_busy) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INTERNAL;
                    end else if (operation_started && spi_done)
                        state_next = ST_ADS1_ID_READ_1;
                    else if (operation_started && counter >= SPI_TIMEOUT_CYCLES - 32'd1) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_SPI_TIMEOUT;
                    end
                end
                ST_ADS1_ID_READ_1: begin
                    if (!operation_started && spi_busy) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INTERNAL;
                    end else if (operation_started && spi_done)
                        state_next = ST_ADS1_ID_READ_2;
                    else if (operation_started && counter >= SPI_TIMEOUT_CYCLES - 32'd1) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_SPI_TIMEOUT;
                    end
                end
                ST_ADS1_ID_READ_2: begin
                    if (!operation_started && spi_busy) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INTERNAL;
                    end else if (operation_started && spi_done)
                        state_next = ST_ADS1_ID_READ_3;
                    else if (operation_started && counter >= SPI_TIMEOUT_CYCLES - 32'd1) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_SPI_TIMEOUT;
                    end
                end
                ST_ADS1_ID_READ_3: begin
                    if (!operation_started && spi_busy) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INTERNAL;
                    end else if (operation_started && spi_done)
                        state_next = ST_ADS2_SDATAC;
                    else if (operation_started && counter >= SPI_TIMEOUT_CYCLES - 32'd1) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_SPI_TIMEOUT;
                    end
                end
                ST_ADS2_SDATAC: begin
                    if (!operation_started && spi_busy) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INTERNAL;
                    end else if (operation_started && spi_done)
                        state_next = ST_ADS2_ID_READ_1;
                    else if (operation_started && counter >= SPI_TIMEOUT_CYCLES - 32'd1) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_SPI_TIMEOUT;
                    end
                end
                ST_ADS2_ID_READ_1: begin
                    if (!operation_started && spi_busy) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INTERNAL;
                    end else if (operation_started && spi_done)
                        state_next = ST_ADS2_ID_READ_2;
                    else if (operation_started && counter >= SPI_TIMEOUT_CYCLES - 32'd1) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_SPI_TIMEOUT;
                    end
                end
                ST_ADS2_ID_READ_2: begin
                    if (!operation_started && spi_busy) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INTERNAL;
                    end else if (operation_started && spi_done)
                        state_next = ST_ADS2_ID_READ_3;
                    else if (operation_started && counter >= SPI_TIMEOUT_CYCLES - 32'd1) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_SPI_TIMEOUT;
                    end
                end
                ST_ADS2_ID_READ_3: begin
                    if (!operation_started && spi_busy) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INTERNAL;
                    end else if (operation_started && spi_done)
                        state_next = ST_ID_RESULT_HOLD;
                    else if (operation_started && counter >= SPI_TIMEOUT_CYCLES - 32'd1) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_SPI_TIMEOUT;
                    end
                end
                ST_ID_RESULT_HOLD: begin
                    if (spi_busy) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INTERNAL;
                    end else if (!ads1_id_valid || !ads2_id_valid) begin
                        state_next = ST_FAULT_SAFE;
                        transition_error = ERROR_INCOMPLETE_ID;
                    end else if (counter >= RESULT_HOLD_CYCLES - 32'd1)
                        state_next = ST_SHUTDOWN_SAFE;
                end
                ST_SHUTDOWN_SAFE: state_next = ST_SHUTDOWN_SAFE;
                ST_FAULT_SAFE:    state_next = ST_FAULT_SAFE;
                default: begin
                    state_next = ST_FAULT_SAFE;
                    transition_error = ERROR_INVALID_STATE;
                end
            endcase
        end
    end

    task automatic launch_spi(
        input logic [1:0] mask,
        input logic [8:0] bits,
        input logic [23:0] tx,
        input logic [31:0] wait_mask,
        input logic [2:0] operation
    );
        begin
            spi_adc_mask             <= mask;
            spi_transfer_bits        <= bits;
            spi_tx_bits              <= tx;
            spi_interbyte_wait_mask  <= wait_mask;
            spi_operation            <= operation;
            spi_start                <= 1'b1;
            operation_started        <= 1'b1;
            counter                  <= 32'd0;
        end
    endtask

    always @(posedge clk) begin
        if (!rst_n) begin
            state_reg                 <= ST_ARM_SAFE;
            counter                   <= 32'd0;
            error_flags               <= 8'd0;
            accepted_event_count      <= 2'd0;
            operation_started         <= 1'b0;
            spi_adc_mask              <= 2'b00;
            spi_tx_bits               <= 24'd0;
            spi_interbyte_wait_mask   <= 32'd0;
            spi_transfer_bits         <= 9'd0;
            spi_start                 <= 1'b0;
            spi_operation             <= SPI_OP_IDLE;
            ads1_id_raw               <= 8'd0;
            ads2_id_raw               <= 8'd0;
            ads1_id_reads             <= 24'd0;
            ads2_id_reads             <= 24'd0;
            ads1_read_valid           <= 3'b000;
            ads2_read_valid           <= 3'b000;
            ads1_any_transport_error  <= 1'b0;
            ads2_any_transport_error  <= 1'b0;
        end else begin
            state_reg <= state_next;
            spi_start <= 1'b0;

            if ((state_next == ST_FAULT_SAFE) && (state_reg != ST_FAULT_SAFE)) begin
                counter           <= 32'd0;
                error_flags       <= error_flags | transition_error;
                operation_started <= 1'b0;
                spi_operation     <= SPI_OP_IDLE;
                if (transition_error == ERROR_DUAL_CS) begin
                    ads1_any_transport_error <= 1'b1;
                    ads2_any_transport_error <= 1'b1;
                end else begin
                    if (state_is_ads1_spi) ads1_any_transport_error <= 1'b1;
                    if (state_is_ads2_spi) ads2_any_transport_error <= 1'b1;
                end
            end else begin
                case (state_reg)
                    ST_ARM_SAFE: begin
                        counter <= 32'd0;
                        operation_started <= 1'b0;
                        spi_operation <= SPI_OP_IDLE;
                        if (key_press_event) accepted_event_count <= 2'd1;
                    end
                    ST_STARTUP_TPOR_WAIT,
                    ST_VCAP_SETTLE_WAIT,
                    ST_RESET_PULSE_LOW,
                    ST_RESET_RELEASE_WAIT,
                    ST_SPI_PREPARE,
                    ST_ID_RESULT_HOLD: begin
                        if (state_next != state_reg) counter <= 32'd0;
                        else counter <= counter + 32'd1;
                        operation_started <= 1'b0;
                        spi_operation <= SPI_OP_IDLE;
                    end
                    ST_ADS1_SDATAC: begin
                        if (!operation_started)
                            launch_spi(2'b01, 9'd8, {16'd0,8'h11}, 32'd0, SPI_OP_ADS1_SDATAC);
                        else if (spi_done) begin
                            operation_started <= 1'b0;
                            spi_operation <= SPI_OP_IDLE;
                            counter <= 32'd0;
                        end else counter <= counter + 32'd1;
                    end
                    ST_ADS1_ID_READ_1,
                    ST_ADS1_ID_READ_2,
                    ST_ADS1_ID_READ_3: begin
                        if (spi_done) begin
                            ads1_id_raw <= spi_adc1_rx_bits[7:0];
                            if (state_reg == ST_ADS1_ID_READ_1) begin
                                ads1_id_reads[7:0] <= spi_adc1_rx_bits[7:0];
                                ads1_read_valid[0] <= 1'b1;
                            end else if (state_reg == ST_ADS1_ID_READ_2) begin
                                ads1_id_reads[15:8] <= spi_adc1_rx_bits[7:0];
                                ads1_read_valid[1] <= 1'b1;
                            end else begin
                                ads1_id_reads[23:16] <= spi_adc1_rx_bits[7:0];
                                ads1_read_valid[2] <= 1'b1;
                            end
                            operation_started <= 1'b0;
                            spi_operation <= SPI_OP_IDLE;
                            counter <= 32'd0;
                        end else if (!operation_started)
                            launch_spi(2'b01, 9'd24, 24'h20_00_00, 32'h0000_0003, SPI_OP_ADS1_RREG);
                        else counter <= counter + 32'd1;
                    end
                    ST_ADS2_SDATAC: begin
                        if (!operation_started)
                            launch_spi(2'b10, 9'd8, {16'd0,8'h11}, 32'd0, SPI_OP_ADS2_SDATAC);
                        else if (spi_done) begin
                            operation_started <= 1'b0;
                            spi_operation <= SPI_OP_IDLE;
                            counter <= 32'd0;
                        end else counter <= counter + 32'd1;
                    end
                    ST_ADS2_ID_READ_1,
                    ST_ADS2_ID_READ_2,
                    ST_ADS2_ID_READ_3: begin
                        if (spi_done) begin
                            ads2_id_raw <= spi_adc2_rx_bits[7:0];
                            if (state_reg == ST_ADS2_ID_READ_1) begin
                                ads2_id_reads[7:0] <= spi_adc2_rx_bits[7:0];
                                ads2_read_valid[0] <= 1'b1;
                            end else if (state_reg == ST_ADS2_ID_READ_2) begin
                                ads2_id_reads[15:8] <= spi_adc2_rx_bits[7:0];
                                ads2_read_valid[1] <= 1'b1;
                            end else begin
                                ads2_id_reads[23:16] <= spi_adc2_rx_bits[7:0];
                                ads2_read_valid[2] <= 1'b1;
                            end
                            operation_started <= 1'b0;
                            spi_operation <= SPI_OP_IDLE;
                            counter <= 32'd0;
                        end else if (!operation_started)
                            launch_spi(2'b10, 9'd24, 24'h20_00_00, 32'h0000_0003, SPI_OP_ADS2_RREG);
                        else counter <= counter + 32'd1;
                    end
                    ST_SHUTDOWN_SAFE,
                    ST_FAULT_SAFE: begin
                        counter <= 32'd0;
                        operation_started <= 1'b0;
                        spi_operation <= SPI_OP_IDLE;
                    end
                    default: begin
                        counter <= 32'd0;
                        operation_started <= 1'b0;
                        spi_operation <= SPI_OP_IDLE;
                    end
                endcase
            end
        end
    end

endmodule
