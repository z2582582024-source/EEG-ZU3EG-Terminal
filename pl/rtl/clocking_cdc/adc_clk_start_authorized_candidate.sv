`timescale 1ns/1ps

// Independent 4J clock-start candidate.  Clock authorization is deliberately
// separate from ADS1299 ID validation: the ADC clock must be available before
// the controller can perform its six ID reads.  This file is verification-only.
module adc_clk_start_authorized_candidate #(
    parameter integer SYS_CLK_HZ = 100_000_000,
    parameter integer HALF_PERIOD_CYCLES = 25
) (
    input  wire clk,
    input  wire rst_n,
    input  wire start_authorized,
    input  wire shutdown_req,
    input  wire fault_req,
    output reg adc_clk,
    output reg [2:0] state,
    output wire shutdown_safe,
    output wire fault_safe,
    output reg start_accepted,
    output reg [31:0] divider_count
);
    localparam [2:0] ST_ARM_SAFE = 3'd0;
    localparam [2:0] ST_RUN = 3'd1;
    localparam [2:0] ST_SHUTDOWN_SAFE = 3'd2;
    localparam [2:0] ST_FAULT_SAFE = 3'd3;
    reg shutdown_pending;

    assign shutdown_safe = state == ST_SHUTDOWN_SAFE;
    assign fault_safe = state == ST_FAULT_SAFE;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            adc_clk <= 1'b0;
            state <= ST_ARM_SAFE;
            start_accepted <= 1'b0;
            divider_count <= 32'd0;
            shutdown_pending <= 1'b0;
        end else if (fault_req) begin
            adc_clk <= 1'b0;
            divider_count <= 32'd0;
            shutdown_pending <= 1'b0;
            state <= ST_FAULT_SAFE;
        end else begin
            case (state)
                ST_ARM_SAFE: begin
                    adc_clk <= 1'b0;
                    divider_count <= 32'd0;
                    shutdown_pending <= 1'b0;
                    if (shutdown_req) begin
                        state <= ST_SHUTDOWN_SAFE;
                    end else if (start_authorized) begin
                        state <= ST_RUN;
                        start_accepted <= 1'b1;
                    end
                end
                ST_RUN: begin
                    if (shutdown_req || shutdown_pending) begin
                        // Normal shutdown completes the active high half-cycle.
                        if (!adc_clk || divider_count == HALF_PERIOD_CYCLES-1) begin
                            adc_clk <= 1'b0;
                            divider_count <= 32'd0;
                            shutdown_pending <= 1'b0;
                            state <= ST_SHUTDOWN_SAFE;
                        end else begin
                            shutdown_pending <= 1'b1;
                            divider_count <= divider_count + 1'b1;
                        end
                    end else if (HALF_PERIOD_CYCLES <= 1) begin
                        divider_count <= 32'd0;
                        adc_clk <= ~adc_clk;
                    end else if (divider_count == HALF_PERIOD_CYCLES-1) begin
                        divider_count <= 32'd0;
                        adc_clk <= ~adc_clk;
                    end else begin
                        divider_count <= divider_count + 1'b1;
                    end
                end
                ST_SHUTDOWN_SAFE: begin
                    adc_clk <= 1'b0;
                    divider_count <= 32'd0;
                end
                ST_FAULT_SAFE: begin
                    adc_clk <= 1'b0;
                    divider_count <= 32'd0;
                end
                default: begin
                    adc_clk <= 1'b0;
                    divider_count <= 32'd0;
                    shutdown_pending <= 1'b0;
                    state <= ST_FAULT_SAFE;
                end
            endcase
        end
    end

    initial begin
        if (SYS_CLK_HZ <= 0 || HALF_PERIOD_CYCLES <= 0)
            $error("invalid ADC clock candidate parameters");
    end
endmodule
