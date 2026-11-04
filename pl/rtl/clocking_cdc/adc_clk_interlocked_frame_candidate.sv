`timescale 1ns/1ps

// Offline-production frame boundary for two ADS1299 devices.
//
// Both CS_N lines are intentionally asserted only for a synchronized data
// frame: the two DOUT streams are clocked in parallel so a pair is atomic.
// Register/control transactions are owned by the production controller and
// use the single-device 4D PHY.  A frame is valid only when both DRDY inputs
// were low before the shared 216-bit transfer began.
module adc_clk_interlocked_frame_candidate #(
    parameter integer SYS_CLK_HZ = 100_000_000,
    parameter integer ADS_CLK_HZ = 2_048_000,
    parameter integer SCLK_HALF_DIV = 50,
    parameter integer PARTIAL_TIMEOUT_CYCLES = 400_000
) (
    input  wire clk,
    input  wire rst_n,
    input  wire enable,
    input  wire adc1_drdy_n,
    input  wire adc2_drdy_n,
    input  wire adc1_dout,
    input  wire adc2_dout,
    output wire adc_sclk,
    output wire adc_din,
    output wire adc1_cs_n,
    output wire adc2_cs_n,
    output wire busy,
    output reg frame_valid,
    output reg partial_frame_event,
    output reg drdy_pair_event,
    output reg [215:0] adc1_frame,
    output reg [215:0] adc2_frame,
    output reg [31:0] total_drdy_events,
    output reg [31:0] partial_frame_count,
    output reg [31:0] desync_count,
    output reg [31:0] timeout_count,
    output reg [8:0] received_bits,
    output wire frame_boundary_safe,
    output wire [1:0] drdy_pair_state,
    output wire frame_accept
);
    reg [1:0] adc1_drdy_sync;
    reg [1:0] adc2_drdy_sync;
    reg phy_start;
    reg armed;
    reg [31:0] partial_counter;
    wire phy_busy;
    wire phy_done;
    wire [215:0] phy_adc1_rx;
    wire [215:0] phy_adc2_rx;
    wire drdy1_low = ~adc1_drdy_sync[1];
    wire drdy2_low = ~adc2_drdy_sync[1];
    wire both_high = ~drdy1_low && ~drdy2_low;
    wire both_low = drdy1_low && drdy2_low;
    wire one_low = drdy1_low ^ drdy2_low;

    // 00=none, 01=ADS1 only, 10=ADS2 only, 11=paired.
    assign drdy_pair_state = {drdy2_low, drdy1_low};
    assign busy = phy_busy;
    // Block a registered pending request when enable is revoked before the
    // PHY acceptance edge; an already busy PHY continues without gating pins.
    assign frame_accept = phy_start && enable && !phy_busy;
    assign frame_boundary_safe = !phy_busy && !phy_start;

    ads1299_spi_phy #(
        .SYS_CLK_HZ(SYS_CLK_HZ),
        .ADS_CLK_HZ(ADS_CLK_HZ),
        .SCLK_HALF_DIV(SCLK_HALF_DIV),
        .MAX_BITS(216)
    ) frame_phy (
        .clk(clk),
        .rst_n(rst_n),
        .start(phy_start && enable),
        .adc_mask(2'b11),
        .transfer_bits(9'd216),
        .tx_bits(216'd0),
        .interbyte_wait_mask(32'd0),
        .adc1_dout(adc1_dout),
        .adc2_dout(adc2_dout),
        .busy(phy_busy),
        .done(phy_done),
        .adc_sclk(adc_sclk),
        .adc_din(adc_din),
        .adc1_cs_n(adc1_cs_n),
        .adc2_cs_n(adc2_cs_n),
        .adc1_rx_bits(phy_adc1_rx),
        .adc2_rx_bits(phy_adc2_rx)
    );

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            adc1_drdy_sync <= 2'b11;
            adc2_drdy_sync <= 2'b11;
            phy_start <= 1'b0;
            armed <= 1'b1;
            partial_counter <= 32'd0;
            frame_valid <= 1'b0;
            partial_frame_event <= 1'b0;
            drdy_pair_event <= 1'b0;
            adc1_frame <= 216'd0;
            adc2_frame <= 216'd0;
            total_drdy_events <= 32'd0;
            partial_frame_count <= 32'd0;
            desync_count <= 32'd0;
            timeout_count <= 32'd0;
            received_bits <= 9'd0;
        end else begin
            adc1_drdy_sync <= {adc1_drdy_sync[0], adc1_drdy_n};
            adc2_drdy_sync <= {adc2_drdy_sync[0], adc2_drdy_n};
            phy_start <= 1'b0;
            frame_valid <= 1'b0;
            partial_frame_event <= 1'b0;
            drdy_pair_event <= 1'b0;

            if (both_high) begin
                armed <= 1'b1;
                partial_counter <= 32'd0;
            end

            if (!enable) begin
                armed <= 1'b1;
                partial_counter <= 32'd0;
            end else if (both_low && armed && !phy_busy) begin
                phy_start <= 1'b1;
                armed <= 1'b0;
                partial_counter <= 32'd0;
                drdy_pair_event <= 1'b1;
                total_drdy_events <= total_drdy_events + 32'd1;
            end else if (one_low && armed && !phy_busy) begin
                if (partial_counter >= PARTIAL_TIMEOUT_CYCLES - 1) begin
                    partial_frame_event <= 1'b1;
                    partial_frame_count <= partial_frame_count + 32'd1;
                    desync_count <= desync_count + 32'd1;
                    timeout_count <= timeout_count + 32'd1;
                    partial_counter <= 32'd0;
                    armed <= 1'b0;
                end else begin
                    partial_counter <= partial_counter + 32'd1;
                end
            end

            if (phy_done) begin
                adc1_frame <= phy_adc1_rx;
                adc2_frame <= phy_adc2_rx;
                received_bits <= 9'd216;
                frame_valid <= 1'b1;
            end
        end
    end
endmodule
