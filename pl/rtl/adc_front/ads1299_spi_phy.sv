`timescale 1ns/1ps

// ADS1299 SPI mode: CPOL=0, CPHA=1, MSB first.
// DOUT changes on SCLK rising edges and is captured here on falling edges.
module ads1299_spi_phy #(
    parameter integer SYS_CLK_HZ = 100_000_000,
    parameter integer ADS_CLK_HZ = 2_048_000,
    parameter integer SCLK_HALF_DIV = 50,
    parameter integer MAX_BITS = 216
) (
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire                    start,
    input  wire [1:0]              adc_mask,
    input  wire [8:0]              transfer_bits,
    input  wire [MAX_BITS-1:0]     tx_bits,
    // Bit n requests a tSDECODE pause after transmitted byte n (zero based).
    // The pause remains inside the same CS_N-low transaction.
    input  wire [31:0]             interbyte_wait_mask,
    input  wire                    adc1_dout,
    input  wire                    adc2_dout,
    output reg                     busy,
    output reg                     done,
    output reg                     adc_sclk,
    output reg                     adc_din,
    output reg                     adc1_cs_n,
    output reg                     adc2_cs_n,
    output reg [MAX_BITS-1:0]      adc1_rx_bits,
    output reg [MAX_BITS-1:0]      adc2_rx_bits
);

    localparam integer T_SCCS_SYS_CYCLES =
        ((4 * SYS_CLK_HZ) + ADS_CLK_HZ - 1) / ADS_CLK_HZ;
    localparam integer T_CSH_SYS_CYCLES =
        ((2 * SYS_CLK_HZ) + ADS_CLK_HZ - 1) / ADS_CLK_HZ;
    localparam integer T_SDECODE_SYS_CYCLES =
        ((4 * SYS_CLK_HZ) + ADS_CLK_HZ - 1) / ADS_CLK_HZ;

    localparam [2:0] ST_IDLE           = 3'd0;
    localparam [2:0] ST_TRANSFER       = 3'd1;
    localparam [2:0] ST_INTERBYTE_WAIT = 3'd2;
    localparam [2:0] ST_HOLD_CS        = 3'd3;
    localparam [2:0] ST_CS_HIGH        = 3'd4;

    reg [2:0] state;
    integer half_counter;
    integer delay_counter;
    integer bit_index;
    integer completed_byte_index;
    reg din_update_pending;
    reg [31:0] interbyte_wait_mask_latched;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= ST_IDLE;
            busy <= 1'b0;
            done <= 1'b0;
            adc_sclk <= 1'b0;
            adc_din <= 1'b0;
            adc1_cs_n <= 1'b1;
            adc2_cs_n <= 1'b1;
            adc1_rx_bits <= {MAX_BITS{1'b0}};
            adc2_rx_bits <= {MAX_BITS{1'b0}};
            half_counter <= 0;
            delay_counter <= 0;
            bit_index <= 0;
            completed_byte_index <= 0;
            din_update_pending <= 1'b0;
            interbyte_wait_mask_latched <= 32'd0;
        end else begin
            done <= 1'b0;
            case (state)
                ST_IDLE: begin
                    busy <= 1'b0;
                    adc_sclk <= 1'b0;
                    adc_din <= 1'b0;
                    adc1_cs_n <= 1'b1;
                    adc2_cs_n <= 1'b1;
                    if (start && adc_mask != 2'b00 &&
                        transfer_bits != 0 && transfer_bits <= MAX_BITS) begin
                        busy <= 1'b1;
                        adc1_cs_n <= ~adc_mask[0];
                        adc2_cs_n <= ~adc_mask[1];
                        adc1_rx_bits <= {MAX_BITS{1'b0}};
                        adc2_rx_bits <= {MAX_BITS{1'b0}};
                        bit_index <= transfer_bits - 1;
                        completed_byte_index <= 0;
                        adc_din <= tx_bits[transfer_bits - 1];
                        din_update_pending <= 1'b0;
                        interbyte_wait_mask_latched <= interbyte_wait_mask;
                        half_counter <= 0;
                        state <= ST_TRANSFER;
                    end
                end

                ST_TRANSFER: begin
                    // Keep the current DIN bit stable through the falling-edge
                    // sample. Update the next bit one system clock later so
                    // the external ADS1299 sees a real setup interval.
                    if (din_update_pending) begin
                        adc_din <= tx_bits[bit_index];
                        din_update_pending <= 1'b0;
                        // Count this setup cycle as part of the low half-period,
                        // preserving an exact 1 MHz SCLK at DIV=50/100 MHz.
                        half_counter <= (SCLK_HALF_DIV > 1) ? 1 : 0;
                    end else if (half_counter == SCLK_HALF_DIV - 1) begin
                        half_counter <= 0;
                        if (!adc_sclk) begin
                            adc_sclk <= 1'b1;
                        end else begin
                            adc_sclk <= 1'b0;
                            if (!adc1_cs_n)
                                adc1_rx_bits[bit_index] <= adc1_dout;
                            if (!adc2_cs_n)
                                adc2_rx_bits[bit_index] <= adc2_dout;
                            if (bit_index == 0) begin
                                delay_counter <= 0;
                                state <= ST_HOLD_CS;
                            end else if (((bit_index & 9'd7) == 0) &&
                                         interbyte_wait_mask_latched[completed_byte_index]) begin
                                // The just-completed byte requested an in-frame
                                // tSDECODE pause.  Preload the next byte MSB,
                                // keep the selected CS_N low, and hold SCLK low.
                                bit_index <= bit_index - 1;
                                completed_byte_index <= completed_byte_index + 1;
                                adc_din <= tx_bits[bit_index - 1];
                                din_update_pending <= 1'b0;
                                delay_counter <= 0;
                                state <= ST_INTERBYTE_WAIT;
                            end else begin
                                bit_index <= bit_index - 1;
                                if ((bit_index & 9'd7) == 0)
                                    completed_byte_index <= completed_byte_index + 1;
                                din_update_pending <= 1'b1;
                            end
                        end
                    end else begin
                        half_counter <= half_counter + 1;
                    end
                end

                ST_INTERBYTE_WAIT: begin
                    adc_sclk <= 1'b0;
                    // CS_N and DIN deliberately remain unchanged.  On the
                    // terminal cycle, launch the next byte's first rising edge;
                    // falling-edge-to-rising-edge separation is exactly the
                    // rounded-up tSDECODE interval.
                    if (delay_counter >= T_SDECODE_SYS_CYCLES - 1) begin
                        adc_sclk <= 1'b1;
                        half_counter <= 0;
                        delay_counter <= 0;
                        state <= ST_TRANSFER;
                    end else begin
                        delay_counter <= delay_counter + 1;
                    end
                end

                ST_HOLD_CS: begin
                    adc_sclk <= 1'b0;
                    if (delay_counter >= T_SCCS_SYS_CYCLES - 1) begin
                        adc1_cs_n <= 1'b1;
                        adc2_cs_n <= 1'b1;
                        adc_din <= 1'b0;
                        din_update_pending <= 1'b0;
                        delay_counter <= 0;
                        state <= ST_CS_HIGH;
                    end else begin
                        delay_counter <= delay_counter + 1;
                    end
                end

                ST_CS_HIGH: begin
                    if (delay_counter >= T_CSH_SYS_CYCLES - 1) begin
                        busy <= 1'b0;
                        done <= 1'b1;
                        state <= ST_IDLE;
                    end else begin
                        delay_counter <= delay_counter + 1;
                    end
                end

                default: state <= ST_IDLE;
            endcase
        end
    end

endmodule
