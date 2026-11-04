`timescale 1ns/1ps

// Time-shared radix-2 FFT with a true dual-port synchronous complex RAM.
// Each butterfly occupies read, multiply, product, result and write cycles;
// sample storage has no asynchronous read or reset, preserving BRAM inference.
module eeg_fft2048_serial_v2 #(
    parameter TWIDDLE_INIT_FILE="twiddle_q15_v2.hex",
    parameter HANN_INIT_FILE="hann_periodic_q15_v2.hex"
)(
    input wire clk,input wire rst_n,input wire flush,input wire in_valid,
    input wire [3:0] in_channel,input wire signed [23:0] in_sample,
    output wire in_ready,output wire busy,output reg input_overrun,
    output wire bin_valid,input wire bin_ready,output wire [10:0] bin_index,
    output wire [3:0] bin_channel,output wire signed [31:0] bin_real,
    output wire signed [31:0] bin_imag
);
    localparam [3:0] COLLECT=0, WINDOW_MUL=1, WINDOW_WRITE=2,
        FFT_READ=3, FFT_MUL=4, FFT_PRODUCT=5, FFT_RESULT=6, FFT_WRITE=7,
        BIN_READ=8, BIN_VALID=9;
    reg [3:0] state=COLLECT;
    reg [10:0] sample_count=0,butterfly=0,output_bin=0;
    reg [3:0] stage=0,channel_latched=0;
    reg signed [23:0] input_sample;
    reg signed [40:0] window_product;
    (* ram_style="block" *) reg [63:0] complex_mem[0:2047];
    (* rom_style="block" *) reg [31:0] twiddle[0:1023];
    (* rom_style="block" *) reg [15:0] hann[0:2047];
    reg [31:0] twiddle_read;
    reg [15:0] hann_read;
    reg [63:0] ram_a_read,ram_b_read;
    reg [10:0] ram_a_addr,ram_b_addr;
    reg [63:0] ram_a_write_data,ram_b_write_data;
    reg ram_a_write,ram_b_write;
    reg signed [47:0] rr,ii,ri,ir;
    reg signed [48:0] product_real,product_imag;
    reg signed [32:0] result_ar,result_ai,result_br,result_bi;
    initial begin
        $readmemh(TWIDDLE_INIT_FILE,twiddle);
        $readmemh(HANN_INIT_FILE,hann);
    end
    function [10:0] reverse11(input [10:0] value);
        integer k;begin for(k=0;k<11;k=k+1)reverse11[k]=value[10-k];end
    endfunction
    wire [10:0] half_span=11'd1<<stage;
    wire [10:0] group_index=butterfly>>stage;
    wire [10:0] within_group=butterfly&(half_span-1'b1);
    wire [11:0] base_index={1'b0,group_index}<<(stage+4'd1);
    wire [10:0] index_a=base_index[10:0]+within_group;
    wire [10:0] index_b=index_a+half_span;
    wire [9:0] twiddle_index=within_group<<(10-stage);
    wire signed [15:0] twiddle_real=twiddle_read[31:16];
    wire signed [15:0] twiddle_imag=twiddle_read[15:0];
    wire signed [31:0] a_real=ram_a_read[63:32],a_imag=ram_a_read[31:0];
    wire signed [31:0] b_real=ram_b_read[63:32],b_imag=ram_b_read[31:0];
    wire signed [31:0] t_real=product_real>>>15,t_imag=product_imag>>>15;
    wire signed [31:0] windowed_sample=window_product>>>15;
    always @* begin
        ram_a_addr=index_a;ram_b_addr=index_b;
        ram_a_write=0;ram_b_write=0;ram_a_write_data=0;ram_b_write_data=0;
        if(state==WINDOW_WRITE)begin
            ram_a_addr=reverse11(sample_count);ram_a_write=1;
            ram_a_write_data={windowed_sample,32'd0};
        end else if(state==FFT_WRITE)begin
            ram_a_write=1;ram_b_write=1;
            ram_a_write_data={result_ar[32:1],result_ai[32:1]};
            ram_b_write_data={result_br[32:1],result_bi[32:1]};
        end else if(state==BIN_READ || state==BIN_VALID)ram_a_addr=output_bin;
    end
    always @(posedge clk)begin
        if(ram_a_write)complex_mem[ram_a_addr]<=ram_a_write_data;
        else ram_a_read<=complex_mem[ram_a_addr];
    end
    always @(posedge clk)begin
        if(ram_b_write)complex_mem[ram_b_addr]<=ram_b_write_data;
        else ram_b_read<=complex_mem[ram_b_addr];
    end
    always @(posedge clk)begin
        twiddle_read<=twiddle[twiddle_index];
        hann_read<=hann[sample_count];
    end
    assign in_ready=state==COLLECT;
    assign busy=state!=COLLECT;
    assign bin_valid=state==BIN_VALID;
    assign bin_index=output_bin;assign bin_channel=channel_latched;
    assign bin_real=ram_a_read[63:32];assign bin_imag=ram_a_read[31:0];
    always @(posedge clk or negedge rst_n)begin
        if(!rst_n)begin
            state<=COLLECT;sample_count<=0;butterfly<=0;output_bin<=0;stage<=0;
            channel_latched<=0;input_overrun<=0;input_sample<=0;window_product<=0;
            rr<=0;ii<=0;ri<=0;ir<=0;product_real<=0;product_imag<=0;
            result_ar<=0;result_ai<=0;result_br<=0;result_bi<=0;
        end else if(flush)begin
            state<=COLLECT;sample_count<=0;butterfly<=0;output_bin<=0;stage<=0;
            channel_latched<=0;input_overrun<=0;
        end else begin
            if(in_valid && !in_ready)input_overrun<=1;
            case(state)
                COLLECT:if(in_valid)begin
                    input_sample<=in_sample;
                    if(sample_count==0)channel_latched<=in_channel;
                    state<=WINDOW_MUL;
                end
                WINDOW_MUL:begin
                    window_product<=$signed(input_sample)*$signed({1'b0,hann_read});
                    state<=WINDOW_WRITE;
                end
                WINDOW_WRITE:begin
                    if(sample_count==2047)begin sample_count<=0;stage<=0;butterfly<=0;state<=FFT_READ;end
                    else begin sample_count<=sample_count+1'b1;state<=COLLECT;end
                end
                FFT_READ:state<=FFT_MUL;
                FFT_MUL:begin
                    rr<=twiddle_real*b_real;ii<=twiddle_imag*b_imag;
                    ri<=twiddle_real*b_imag;ir<=twiddle_imag*b_real;state<=FFT_PRODUCT;
                end
                FFT_PRODUCT:begin product_real<=rr-ii;product_imag<=ri+ir;state<=FFT_RESULT;end
                FFT_RESULT:begin
                    result_ar<={a_real[31],a_real}+{t_real[31],t_real};
                    result_ai<={a_imag[31],a_imag}+{t_imag[31],t_imag};
                    result_br<={a_real[31],a_real}-{t_real[31],t_real};
                    result_bi<={a_imag[31],a_imag}-{t_imag[31],t_imag};state<=FFT_WRITE;
                end
                FFT_WRITE:begin
                    if(butterfly==1023)begin
                        butterfly<=0;
                        if(stage==10)begin output_bin<=0;state<=BIN_READ;end
                        else begin stage<=stage+1'b1;state<=FFT_READ;end
                    end else begin butterfly<=butterfly+1'b1;state<=FFT_READ;end
                end
                BIN_READ:state<=BIN_VALID;
                BIN_VALID:if(bin_ready)begin
                    if(output_bin==1024)begin output_bin<=0;sample_count<=0;state<=COLLECT;end
                    else begin output_bin<=output_bin+1'b1;state<=BIN_READ;end
                end
                default:begin state<=COLLECT;sample_count<=0;input_overrun<=1;end
            endcase
        end
    end
endmodule
