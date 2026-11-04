`timescale 1ns/1ps

// Waits until both independent DRDY inputs are low, then clocks exactly one
// 216-bit frame from both DOUT pins in parallel with shared SCLK and DIN.
module ads1299_dual_frame_rx #(
    parameter integer SYS_CLK_HZ = 100_000_000,
    parameter integer ADS_CLK_HZ = 2_048_000,
    parameter integer SCLK_HALF_DIV = 50,
    parameter integer PARTIAL_TIMEOUT_CYCLES = 400_000
) (
    input  wire          clk,
    input  wire          rst_n,
    input  wire          enable,
    input  wire          adc1_drdy_n,
    input  wire          adc2_drdy_n,
    input  wire          adc1_dout,
    input  wire          adc2_dout,
    output wire          adc_sclk,
    output wire          adc_din,
    output wire          adc1_cs_n,
    output wire          adc2_cs_n,
    output reg           frame_valid,
    output reg           partial_drdy_timeout,
    output reg [215:0]   adc1_frame,
    output reg [215:0]   adc2_frame
);

    reg [1:0] adc1_drdy_sync;
    reg [1:0] adc2_drdy_sync;
    reg phy_start;
    reg armed;
    integer partial_counter;
    wire phy_busy;
    wire phy_done;
    wire [215:0] phy_adc1_rx;
    wire [215:0] phy_adc2_rx;
    wire [1:0] drdy_low = {~adc2_drdy_sync[1], ~adc1_drdy_sync[1]};

    ads1299_spi_phy #(
        .SYS_CLK_HZ(SYS_CLK_HZ),
        .ADS_CLK_HZ(ADS_CLK_HZ),
        .SCLK_HALF_DIV(SCLK_HALF_DIV),
        .MAX_BITS(216)
    ) phy (
        .clk(clk),
        .rst_n(rst_n),
        .start(phy_start),
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
            partial_counter <= 0;
            frame_valid <= 1'b0;
            partial_drdy_timeout <= 1'b0;
            adc1_frame <= 216'd0;
            adc2_frame <= 216'd0;
        end else begin
            adc1_drdy_sync <= {adc1_drdy_sync[0], adc1_drdy_n};
            adc2_drdy_sync <= {adc2_drdy_sync[0], adc2_drdy_n};
            phy_start <= 1'b0;
            frame_valid <= 1'b0;
            partial_drdy_timeout <= 1'b0;

            if (drdy_low == 2'b00 && !phy_busy)
                armed <= 1'b1;

            if (!enable) begin
                partial_counter <= 0;
            end else if (drdy_low == 2'b11 && armed && !phy_busy) begin
                phy_start <= 1'b1;
                armed <= 1'b0;
                partial_counter <= 0;
            end else if ((drdy_low == 2'b01 || drdy_low == 2'b10) && !phy_busy) begin
                if (partial_counter >= PARTIAL_TIMEOUT_CYCLES - 1) begin
                    partial_drdy_timeout <= 1'b1;
                    partial_counter <= 0;
                end else begin
                    partial_counter <= partial_counter + 1;
                end
            end else begin
                partial_counter <= 0;
            end

            if (phy_done) begin
                adc1_frame <= phy_adc1_rx;
                adc2_frame <= phy_adc2_rx;
                frame_valid <= 1'b1;
            end
        end
    end

endmodule
