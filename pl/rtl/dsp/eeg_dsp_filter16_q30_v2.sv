`timescale 1ns/1ps

module eeg_dsp_filter16_q30_v2(
    input wire clk, input wire rst_n, input wire flush,
    input wire in_valid, input wire in_clipped,
    input wire [3:0] in_channel, input wire signed [23:0] in_sample,
    output wire out_valid, output wire out_clipped,
    output wire [3:0] out_channel, output wire signed [23:0] out_sample
);
    wire v1,v2,c1,c2;
    wire [3:0] ch1,ch2;
    wire signed [23:0] x1,x2;
    eeg_biquad_q30_v2 #(
        .B0(32'sd1064016134),.B1(-32'sd2128032269),.B2(32'sd1064016134),
        .A1(-32'sd2127944174),.A2(32'sd1054378540)
    ) highpass(.clk(clk),.rst_n(rst_n),.flush(flush),.in_valid(in_valid),.in_clipped(in_clipped),
        .in_channel(in_channel),.in_sample(in_sample),.out_valid(v1),.out_clipped(c1),
        .out_channel(ch1),.out_sample(x1));
    eeg_biquad_q30_v2 #(
        .B0(32'sd1056833504),.B1(-32'sd592244478),.B2(32'sd1056833504),
        .A1(-32'sd592244478),.A2(32'sd1039925183)
    ) notch(.clk(clk),.rst_n(rst_n),.flush(flush),.in_valid(v1),.in_clipped(c1),
        .in_channel(ch1),.in_sample(x1),.out_valid(v2),.out_clipped(c2),
        .out_channel(ch2),.out_sample(x2));
    eeg_biquad_q30_v2 #(
        .B0(32'sd195145852),.B1(32'sd390291704),.B2(32'sd195145852),
        .A1(-32'sd522703187),.A2(32'sd229544771)
    ) lowpass(.clk(clk),.rst_n(rst_n),.flush(flush),.in_valid(v2),.in_clipped(c2),
        .in_channel(ch2),.in_sample(x2),.out_valid(out_valid),.out_clipped(out_clipped),
        .out_channel(out_channel),.out_sample(out_sample));
endmodule
