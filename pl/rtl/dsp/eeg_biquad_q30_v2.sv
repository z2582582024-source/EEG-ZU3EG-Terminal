`timescale 1ns/1ps
// Five-cycle, 16-channel interleaved Q30 transposed biquad. A channel must
// not recur within five clocks; the production filter feeds 0..15 in order.
// Narrow 24x32 multipliers use DSP resources and preserve exact 64-bit state,
// round-away-from-zero Q30 and S24 saturation semantics of the golden model.
module eeg_biquad_q30_v2 #(
    parameter signed [31:0] B0=0,B1=0,B2=0,A1=0,A2=0
)(
    input wire clk,input wire rst_n,input wire flush,
    input wire in_valid,input wire in_clipped,input wire [3:0] in_channel,
    input wire signed [23:0] in_sample,
    output reg out_valid,output reg out_clipped,output reg [3:0] out_channel,
    output reg signed [23:0] out_sample
);
    localparam signed [63:0] Q30_HALF=64'sd536870912;
    localparam signed [63:0] Q30_MAX=64'sd8388607,Q30_MIN=-64'sd8388608;
    reg signed [63:0] z1[0:15],z2[0:15];
    reg v1=0,v2=0,v3=0,v4=0;
    reg c1,c2,c3,c4;
    reg [3:0] ch1,ch2,ch3,ch4;
    (* use_dsp="yes" *) reg signed [55:0] p0_1,p1_1,p2_1,pa1_4,pa2_4;
    reg signed [55:0] p1_2,p2_2,p1_3,p2_3,p1_4,p2_4;
    reg signed [63:0] z1_1,z2_1,z2_2,z2_3,z2_4,acc2;
    reg signed [23:0] y3,y4;
    wire signed [63:0] rounded=acc2>=0?((acc2+Q30_HALF)>>>30):-(((-acc2+Q30_HALF)>>>30));
    wire signed [23:0] saturated=rounded>Q30_MAX?24'sh7fffff:(rounded<Q30_MIN?24'sh800000:rounded[23:0]);
    wire clipped=(rounded>Q30_MAX)||(rounded<Q30_MIN);
    wire signed [63:0] bp1={{8{p1_4[55]}},p1_4},bp2={{8{p2_4[55]}},p2_4};
    wire signed [63:0] ap1={{8{pa1_4[55]}},pa1_4},ap2={{8{pa2_4[55]}},pa2_4};
    integer i;
    always @(posedge clk or negedge rst_n)begin
        if(!rst_n)begin
            v1<=0;v2<=0;v3<=0;v4<=0;out_valid<=0;out_clipped<=0;out_channel<=0;out_sample<=0;
            c1<=0;c2<=0;c3<=0;c4<=0;ch1<=0;ch2<=0;ch3<=0;ch4<=0;
            p0_1<=0;p1_1<=0;p2_1<=0;pa1_4<=0;pa2_4<=0;
            p1_2<=0;p2_2<=0;p1_3<=0;p2_3<=0;p1_4<=0;p2_4<=0;
            z1_1<=0;z2_1<=0;z2_2<=0;z2_3<=0;z2_4<=0;acc2<=0;y3<=0;y4<=0;
            for(i=0;i<16;i=i+1)begin z1[i]<=0;z2[i]<=0;end
        end else if(flush)begin
            v1<=0;v2<=0;v3<=0;v4<=0;out_valid<=0;out_clipped<=0;out_channel<=0;out_sample<=0;
            for(i=0;i<16;i=i+1)begin z1[i]<=0;z2[i]<=0;end
        end else begin
            v1<=in_valid;v2<=v1;v3<=v2;v4<=v3;out_valid<=v4;
            if(in_valid)begin
                p0_1<=$signed(B0)*$signed(in_sample);
                p1_1<=$signed(B1)*$signed(in_sample);
                p2_1<=$signed(B2)*$signed(in_sample);
                z1_1<=z1[in_channel];z2_1<=z2[in_channel];ch1<=in_channel;c1<=in_clipped;
            end
            if(v1)begin
                acc2<={{8{p0_1[55]}},p0_1}+z1_1;
                p1_2<=p1_1;p2_2<=p2_1;z2_2<=z2_1;ch2<=ch1;c2<=c1;
            end
            if(v2)begin
                y3<=saturated;p1_3<=p1_2;p2_3<=p2_2;z2_3<=z2_2;ch3<=ch2;c3<=c2|clipped;
            end
            if(v3)begin
                pa1_4<=$signed(A1)*$signed(y3);pa2_4<=$signed(A2)*$signed(y3);
                p1_4<=p1_3;p2_4<=p2_3;z2_4<=z2_3;ch4<=ch3;c4<=c3;y4<=y3;
            end
            if(v4)begin
                z1[ch4]<=bp1-ap1+z2_4;z2[ch4]<=bp2-ap2;
                out_sample<=y4;out_channel<=ch4;out_clipped<=c4;
            end
        end
    end
endmodule
