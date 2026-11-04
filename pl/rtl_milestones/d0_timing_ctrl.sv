`timescale 1ns/1ps

// Milestone 4B D0-only PWDN/RESET sequencer.
//
// IMPORTANT CLOCK BOUNDARY:
// The frozen eleven-signal ADS interface does not route the acquisition-board
// ADC_CLK/RF1 signal into the PL.  This controller is therefore timed by the
// confirmed 100 MHz P5B PL clock.  ADC_CLK must already be present and must be
// verified at RF1 with an oscilloscope after this design is downloaded.  The
// controller remains in ARM_SAFE indefinitely until the operator presses the
// dedicated P5B PL_KEY0 button.  The button is an operator interlock, not an
// ADC_CLK detector.
//
// No SPI PHY, VIO, command path, register access, ID read, or START action is
// implemented.  DIN, SCLK, START, and both CS_N outputs remain zero forever.
module d0_timing_ctrl #(
    parameter integer ARM_DEBOUNCE_CYCLES       = 1_000_000,   // 10.000 ms @ 100 MHz
    parameter integer TPOR_WAIT_CYCLES          = 20_000_000,  // 200.000 ms
    parameter integer RESET_LOW_CYCLES          = 100_000,     // 1.000 ms
    parameter integer RESET_RELEASE_WAIT_CYCLES = 100_000      // 1.000 ms
) (
    input  wire        clk,
    input  wire        arm_start_n,
    input  wire [3:0]  raw_inputs,
    output reg  [2:0]  state,
    output reg  [31:0] counter,
    output reg  [7:0]  error_flags,
    output wire        done,
    output wire        arm_start_synchronized,
    output reg         arm_ready,
    output reg         arm_start_seen,
    output wire [3:0]  synchronized_inputs,
    output wire [6:0]  control_outputs,
    output wire        adc_din,
    output wire        adc_pwdn_n,
    output wire        adc_reset_n,
    output wire        adc_sclk,
    output wire        adc_start,
    output wire        adc1_cs_n,
    output wire        adc2_cs_n
);

    localparam [2:0] ST_ARM_SAFE           = 3'd0;
    localparam [2:0] ST_TPOR_WAIT          = 3'd1;
    localparam [2:0] ST_RESET_LOW          = 3'd2;
    localparam [2:0] ST_RESET_RELEASE_WAIT = 3'd3;
    localparam [2:0] ST_DONE               = 3'd4;
    localparam [2:0] ST_FAULT              = 3'd5;

    (* ASYNC_REG = "TRUE" *) reg [1:0] input_sync_0 = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] input_sync_1 = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] input_sync_2 = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] input_sync_3 = 2'b00;
    // Initialize low so an active-low key held during configuration cannot be
    // mistaken for a sampled released state.  ARM becomes ready only after a
    // real synchronized high level has arrived from the board pull-up.
    (* ASYNC_REG = "TRUE" *) reg [1:0] arm_start_sync_ff = 2'b00;
    reg [31:0] arm_debounce_counter = 32'd0;

    initial begin
        state       = ST_ARM_SAFE;
        counter     = 32'd0;
        error_flags = 8'd0;
        arm_ready   = 1'b0;
        arm_start_seen = 1'b0;
    end

    assign synchronized_inputs = {
        input_sync_3[1], input_sync_2[1], input_sync_1[1], input_sync_0[1]
    };
    assign arm_start_synchronized = arm_start_sync_ff[1];

    // Bit order: DIN, PWDN_N, RESET_N, SCLK, START, ADC1_CS_N, ADC2_CS_N.
    // RESET_N stays low while tPOR is observed after PWDN_N release, remains
    // low for the explicit RESET_LOW interval, and is finally released high.
    // Invalid-state handling returns all ADS-facing outputs low.
    assign adc_din     = 1'b0;
    assign adc_pwdn_n  = (state == ST_TPOR_WAIT) ||
                         (state == ST_RESET_LOW) ||
                         (state == ST_RESET_RELEASE_WAIT) ||
                         (state == ST_DONE);
    assign adc_reset_n = (state == ST_RESET_RELEASE_WAIT) ||
                         (state == ST_DONE);
    assign adc_sclk    = 1'b0;
    assign adc_start   = 1'b0;
    assign adc1_cs_n   = 1'b0;
    assign adc2_cs_n   = 1'b0;
    assign control_outputs = {
        adc_din, adc_pwdn_n, adc_reset_n, adc_sclk,
        adc_start, adc1_cs_n, adc2_cs_n
    };
    assign done = (state == ST_DONE);

    always @(posedge clk) begin
        input_sync_0 <= {input_sync_0[0], raw_inputs[0]};
        input_sync_1 <= {input_sync_1[0], raw_inputs[1]};
        input_sync_2 <= {input_sync_2[0], raw_inputs[2]};
        input_sync_3 <= {input_sync_3[0], raw_inputs[3]};
        arm_start_sync_ff <= {arm_start_sync_ff[0], arm_start_n};

        case (state)
            ST_ARM_SAFE: begin
                counter <= 32'd0;

                // Require a sampled released state after configuration before
                // accepting a press.  A button held during bitstream download
                // therefore cannot start D0 when configuration completes.
                if (!arm_ready) begin
                    if (arm_start_sync_ff[1]) begin
                        if (arm_debounce_counter >= ARM_DEBOUNCE_CYCLES - 1) begin
                            arm_debounce_counter <= 32'd0;
                            arm_ready            <= 1'b1;
                        end else begin
                            arm_debounce_counter <= arm_debounce_counter + 32'd1;
                        end
                    end else begin
                        arm_debounce_counter <= 32'd0;
                    end
                end else if (!arm_start_sync_ff[1]) begin
                    if (arm_debounce_counter >= ARM_DEBOUNCE_CYCLES - 1) begin
                        arm_debounce_counter <= 32'd0;
                        arm_start_seen       <= 1'b1;
                        state                <= ST_TPOR_WAIT;
                    end else begin
                        arm_debounce_counter <= arm_debounce_counter + 32'd1;
                    end
                end else begin
                    arm_debounce_counter <= 32'd0;
                end
            end
            ST_TPOR_WAIT: begin
                arm_debounce_counter <= 32'd0;
                if (counter >= TPOR_WAIT_CYCLES - 1) begin
                    counter <= 32'd0;
                    state   <= ST_RESET_LOW;
                end else begin
                    counter <= counter + 32'd1;
                end
            end
            ST_RESET_LOW: begin
                arm_debounce_counter <= 32'd0;
                if (counter >= RESET_LOW_CYCLES - 1) begin
                    counter <= 32'd0;
                    state   <= ST_RESET_RELEASE_WAIT;
                end else begin
                    counter <= counter + 32'd1;
                end
            end
            ST_RESET_RELEASE_WAIT: begin
                arm_debounce_counter <= 32'd0;
                if (counter >= RESET_RELEASE_WAIT_CYCLES - 1) begin
                    counter <= 32'd0;
                    state   <= ST_DONE;
                end else begin
                    counter <= counter + 32'd1;
                end
            end
            ST_DONE: begin
                arm_debounce_counter <= 32'd0;
                counter <= 32'd0;
                state   <= ST_DONE;
            end
            ST_FAULT: begin
                arm_debounce_counter <= 32'd0;
                counter     <= 32'd0;
                state       <= ST_FAULT;
                error_flags <= error_flags | 8'h01;
            end
            default: begin
                arm_debounce_counter <= 32'd0;
                counter     <= 32'd0;
                state       <= ST_FAULT;
                error_flags <= error_flags | 8'h01;
            end
        endcase
    end

endmodule
