`timescale 1ns/1ps
// CANDIDATE (simulation verified only). 4:1 anti-alias decimator for the 976.5625 SPS front end.
//
// 16 channels, TAPS-tap linear-phase FIR (Q1.17, DC gain exactly 1.0), output rate = input rate / 4
// (976.5625 -> 244.140625 SPS). One DSP48-class multiplier is time-shared over the 16*TAPS MACs of a
// decimation step (~16*TAPS+8 clocks = ~10 us against a 4.096 ms output period), so the whole block
// costs one multiplier, one 1024x24 ring RAM and a 60x18 coefficient ROM.
//
// Bit-exact contract (golden: golden/mr_golden.py Decim4):
//   y = sat24( round_half_away( sum_k coef[k] * x[newest-k] / 2^17 ) ), x before start = 0,
//   a decimated frame is produced when every 4th input frame completes (frames 4,8,12,... since flush).
// Samples are written channel 0..15 of a frame in order; a frame may only be started while in_ready=1.
// in_ready drops when the 4th frame completes and returns when the 16 results have been emitted.
module eeg_decim4_fir_v3 #(
    parameter integer TAPS = 60,                       // <= 64 (ring depth)
    parameter COEF_FILE = "dec_coef.hex"
)(
    input  wire clk, input wire rst_n, input wire flush,
    input  wire in_valid, input wire [3:0] in_channel, input wire signed [23:0] in_sample,
    output wire in_ready,
    output reg  out_valid, output reg [3:0] out_channel, output reg signed [23:0] out_sample,
    output reg  out_sat,                               // this result saturated to S24
    output reg  out_warm,                              // TAPS input frames have been seen since flush
    output reg  out_last,                              // channel 15 of the decimated frame
    output reg  [31:0] frames_in
);
    localparam [1:0] S_IDLE = 2'd0, S_RUN = 2'd1, S_DRAIN = 2'd2;
    reg [1:0] state;
    assign in_ready = (state == S_IDLE);

    (* ram_style = "block" *) reg signed [23:0] ring [0:1023];
    reg signed [17:0] coef [0:63];
    initial $readmemh(COEF_FILE, coef);

    reg [5:0] wp;                                      // slot the next frame is written to
    reg [5:0] newest;                                  // slot of the most recent complete frame
    reg [1:0] fcnt;
    reg [6:0] seen;                                    // frames since flush, saturating at 64
    reg [6:0] seen_snap;

    // stage 0: address issue
    reg [3:0] ch_c; reg [5:0] k;
    // stage 1: BRAM / ROM registered
    reg s1_v, s1_last, s1_first, s1_mask; reg [3:0] s1_ch;
    reg signed [23:0] s1_x; reg signed [17:0] s1_c;
    // stage 2: product
    reg s2_v, s2_last, s2_first; reg [3:0] s2_ch;
    reg signed [41:0] s2_p;
    // stage 3: accumulate
    reg s3_v; reg [3:0] s3_ch;
    reg signed [49:0] acc;
    reg s3_done;

    wire [5:0] rd_slot = newest - k;
    wire [9:0] rd_addr = {ch_c, rd_slot};
    wire issue = (state == S_RUN);
    wire last_issue = issue && (k == TAPS - 1);

    // rounding / saturation of the finished accumulator (half away from zero, >>17)
    wire signed [49:0] half = 50'sd65536;
    wire signed [49:0] absacc = acc[49] ? -acc : acc;
    wire signed [49:0] rabs = (absacc + half) >>> 17;
    wire signed [49:0] rounded = acc[49] ? -rabs : rabs;
    wire over = (rounded > 50'sd8388607) || (rounded < -50'sd8388608);
    wire signed [23:0] satval = (rounded > 50'sd8388607) ? 24'sh7fffff :
                                (rounded < -50'sd8388608) ? 24'sh800000 : rounded[23:0];

    always @(posedge clk) begin
        if (in_valid && in_ready) ring[{in_channel, wp}] <= in_sample;
        s1_x <= ring[rd_addr];
        s1_c <= coef[k];
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE; wp <= 0; newest <= 0; fcnt <= 0; seen <= 0; seen_snap <= 0; frames_in <= 0;
            ch_c <= 0; k <= 0;
            s1_v <= 0; s1_last <= 0; s1_first <= 0; s1_mask <= 0; s1_ch <= 0;
            s2_v <= 0; s2_last <= 0; s2_first <= 0; s2_ch <= 0; s2_p <= 0;
            s3_v <= 0; s3_ch <= 0; acc <= 0; s3_done <= 0;
            out_valid <= 0; out_channel <= 0; out_sample <= 0; out_sat <= 0; out_warm <= 0; out_last <= 0;
        end else if (flush) begin
            state <= S_IDLE; wp <= 0; newest <= 0; fcnt <= 0; seen <= 0; frames_in <= 0; ch_c <= 0; k <= 0;
            s1_v <= 0; s2_v <= 0; s3_v <= 0; s3_done <= 0; out_valid <= 0;
        end else begin
            out_valid <= 1'b0;
            // ---- frame capture
            if (in_valid && in_ready && in_channel == 4'd15) begin
                wp <= wp + 1'b1; newest <= wp;
                frames_in <= frames_in + 1'b1;
                if (seen != 7'd64) seen <= seen + 1'b1;
                fcnt <= fcnt + 1'b1;
                if (fcnt == 2'd3) begin
                    state <= S_RUN; ch_c <= 0; k <= 0;
                    seen_snap <= (seen != 7'd64) ? seen + 1'b1 : seen;
                end
            end
            // ---- stage 0 -> 1 control
            s1_v <= issue; s1_last <= last_issue; s1_first <= (k == 0); s1_ch <= ch_c;
            s1_mask <= (k >= seen_snap);
            if (issue) begin
                if (k == TAPS - 1) begin
                    k <= 0;
                    if (ch_c == 4'd15) state <= S_DRAIN; else ch_c <= ch_c + 1'b1;
                end else k <= k + 1'b1;
            end
            // ---- stage 2: product
            s2_v <= s1_v; s2_last <= s1_last; s2_first <= s1_first; s2_ch <= s1_ch;
            s2_p <= s1_mask ? 42'sd0 : (s1_x * s1_c);
            // ---- stage 3: accumulate
            s3_done <= 1'b0;
            if (s2_v) begin
                acc <= (s2_first ? 50'sd0 : acc) + {{8{s2_p[41]}}, s2_p};
                s3_ch <= s2_ch;
                if (s2_last) s3_done <= 1'b1;
            end
            // ---- stage 4: result
            if (s3_done) begin
                out_valid <= 1'b1; out_channel <= s3_ch; out_sample <= satval; out_sat <= over;
                out_warm <= (seen_snap >= TAPS); out_last <= (s3_ch == 4'd15);
                if (s3_ch == 4'd15) state <= S_IDLE;
            end
        end
    end
endmodule
