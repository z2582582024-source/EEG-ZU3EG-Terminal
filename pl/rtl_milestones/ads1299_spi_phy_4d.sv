`timescale 1ns/1ps

// 4D-isolated ADS1299 SPI PHY.  It retains the proven CPOL=0/CPHA=1 and
// tSDECODE/tSCCS/tCSH behavior, but deliberately moves next-byte DIN preload
// off the byte-ending falling edge and into the following SCLK-low wait.
// This matters for non-zero WREG payloads and leaves the frozen 4C PHY/source
// and all existing 4C artifacts untouched.
module ads1299_spi_phy_4d #(
    parameter integer SYS_CLK_HZ=100_000_000, ADS_CLK_HZ=2_048_000,
    parameter integer SCLK_HALF_DIV=50, MAX_BITS=216
)(
    input wire clk,input wire rst_n,input wire start,input wire [1:0] adc_mask,
    input wire [8:0] transfer_bits,input wire [MAX_BITS-1:0] tx_bits,
    input wire [31:0] interbyte_wait_mask,input wire adc1_dout,input wire adc2_dout,
    output reg busy,output reg done,output reg adc_sclk,output reg adc_din,
    output reg adc1_cs_n,output reg adc2_cs_n,
    output reg [MAX_BITS-1:0] adc1_rx_bits,output reg [MAX_BITS-1:0] adc2_rx_bits
);
    localparam integer T_SCCS=((4*SYS_CLK_HZ)+ADS_CLK_HZ-1)/ADS_CLK_HZ;
    localparam integer T_CSH=((2*SYS_CLK_HZ)+ADS_CLK_HZ-1)/ADS_CLK_HZ;
    localparam integer T_SDECODE=((4*SYS_CLK_HZ)+ADS_CLK_HZ-1)/ADS_CLK_HZ;
    localparam [2:0] IDLE=0,TRANSFER=1,INTERBYTE=2,HOLD_CS=3,CS_HIGH=4;
    reg [2:0] state; integer half_counter,delay_counter,bit_index,completed_byte_index;
    reg din_update_pending; reg [31:0] wait_mask_latched;
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            state<=IDLE;busy<=0;done<=0;adc_sclk<=0;adc_din<=0;adc1_cs_n<=1;adc2_cs_n<=1;
            adc1_rx_bits<={MAX_BITS{1'b0}};adc2_rx_bits<={MAX_BITS{1'b0}};
            half_counter<=0;delay_counter<=0;bit_index<=0;completed_byte_index<=0;
            din_update_pending<=0;wait_mask_latched<=0;
        end else begin
            done<=0;
            case(state)
                IDLE: begin
                    busy<=0;adc_sclk<=0;adc_din<=0;adc1_cs_n<=1;adc2_cs_n<=1;
                    if(start&&adc_mask!=0&&transfer_bits!=0&&transfer_bits<=MAX_BITS) begin
                        busy<=1;adc1_cs_n<=~adc_mask[0];adc2_cs_n<=~adc_mask[1];
                        adc1_rx_bits<={MAX_BITS{1'b0}};adc2_rx_bits<={MAX_BITS{1'b0}};
                        bit_index<=transfer_bits-1;completed_byte_index<=0;
                        adc_din<=tx_bits[transfer_bits-1];din_update_pending<=0;
                        wait_mask_latched<=interbyte_wait_mask;half_counter<=0;state<=TRANSFER;
                    end
                end
                TRANSFER: begin
                    if(din_update_pending) begin
                        adc_din<=tx_bits[bit_index];din_update_pending<=0;
                        half_counter<=SCLK_HALF_DIV>1?1:0;
                    end else if(half_counter==SCLK_HALF_DIV-1) begin
                        half_counter<=0;
                        if(!adc_sclk) adc_sclk<=1;
                        else begin
                            adc_sclk<=0;
                            if(!adc1_cs_n)adc1_rx_bits[bit_index]<=adc1_dout;
                            if(!adc2_cs_n)adc2_rx_bits[bit_index]<=adc2_dout;
                            if(bit_index==0) begin delay_counter<=0;state<=HOLD_CS;end
                            else if(((bit_index&9'd7)==0)&&wait_mask_latched[completed_byte_index]) begin
                                bit_index<=bit_index-1;completed_byte_index<=completed_byte_index+1;
                                // Do not change DIN on this sampling edge.
                                delay_counter<=0;state<=INTERBYTE;
                            end else begin
                                bit_index<=bit_index-1;
                                if((bit_index&9'd7)==0)completed_byte_index<=completed_byte_index+1;
                                din_update_pending<=1;
                            end
                        end
                    end else half_counter<=half_counter+1;
                end
                INTERBYTE: begin
                    adc_sclk<=0;adc_din<=tx_bits[bit_index];
                    if(delay_counter>=T_SDECODE-1) begin
                        adc_sclk<=1;half_counter<=0;delay_counter<=0;state<=TRANSFER;
                    end else delay_counter<=delay_counter+1;
                end
                HOLD_CS: begin
                    adc_sclk<=0;
                    if(delay_counter>=T_SCCS-1) begin
                        adc1_cs_n<=1;adc2_cs_n<=1;adc_din<=0;din_update_pending<=0;
                        delay_counter<=0;state<=CS_HIGH;
                    end else delay_counter<=delay_counter+1;
                end
                CS_HIGH: begin
                    if(delay_counter>=T_CSH-1) begin busy<=0;done<=1;state<=IDLE;end
                    else delay_counter<=delay_counter+1;
                end
                default:state<=IDLE;
            endcase
        end
    end
endmodule
