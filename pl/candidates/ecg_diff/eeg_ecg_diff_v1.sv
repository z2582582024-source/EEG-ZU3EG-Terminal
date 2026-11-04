`timescale 1ns/1ps
// CANDIDATE, simulation only. Not part of any release, bitstream, manifest or qualification.
//
// Post-filter bipolar derivation for the EEG14_ECG2 mode: it taps the output of the
// existing 16-channel Q30 engine (eeg_dsp_filter16_q30_v2), waits until both ECG role
// samples of a frame have been seen, and emits SIGN*(B-A) as a signed 25-bit value.
// The two S24 filter outputs are never narrowed: a 24-bit subtract can need 25 bits.
//
// Ordering note: filtering A and B with the same response and subtracting afterwards is
// equal to subtracting first and filtering once (the filters are linear) up to Q30 rounding;
// the C++ golden model reports that difference. Doing it after the engine needs no change
// to the validated 24-bit biquad datapath.
module eeg_ecg_diff_v1 #(
    parameter [3:0] ROLE_A = 4'd0,
    parameter [3:0] ROLE_B = 4'd1,
    parameter       NEGATE = 1'b0            // 0: B-A, 1: A-B
)(
    input  wire clk, input wire rst_n, input wire flush,
    input  wire mode_ecg,                    // 0 = EEG16: this block is idle and emits nothing
    input  wire frame_end,                   // one pulse per 16-channel frame (after channel 15)
    input  wire in_valid, input wire in_clipped,
    input  wire [3:0] in_channel, input wire signed [23:0] in_sample,
    output reg  out_valid,
    output reg  signed [24:0] out_diff,
    output reg  out_clipped,                 // processed clipping of either role channel
    output reg  [31:0] pair_count,           // derived samples emitted
    output reg  [15:0] err_pair_overrun,     // a role sample arrived twice before its partner
    output reg  [15:0] err_pair_missing      // frame ended with one role sample unpaired
);
    reg signed [23:0] a_val, b_val;
    reg a_have, b_have, a_clip, b_clip;
    wire hit_a = in_valid && mode_ecg && (in_channel == ROLE_A);
    wire hit_b = in_valid && mode_ecg && (in_channel == ROLE_B);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            out_valid <= 1'b0; out_diff <= 25'sd0; out_clipped <= 1'b0;
            pair_count <= 32'd0; err_pair_overrun <= 16'd0; err_pair_missing <= 16'd0;
            a_val <= 24'sd0; b_val <= 24'sd0; a_have <= 1'b0; b_have <= 1'b0; a_clip <= 1'b0; b_clip <= 1'b0;
        end else if (flush || !mode_ecg) begin
            out_valid <= 1'b0; a_have <= 1'b0; b_have <= 1'b0;
        end else begin
            out_valid <= 1'b0;
            if (hit_a) begin
                if (a_have && !b_have) err_pair_overrun <= err_pair_overrun + 16'd1;
                a_val <= in_sample; a_clip <= in_clipped; a_have <= 1'b1;
            end
            if (hit_b) begin
                if (b_have && !a_have) err_pair_overrun <= err_pair_overrun + 16'd1;
                b_val <= in_sample; b_clip <= in_clipped; b_have <= 1'b1;
            end
            // Both present: derive on the cycle after the second one is registered.
            if (a_have && b_have) begin
                out_diff    <= NEGATE ? ($signed({a_val[23], a_val}) - $signed({b_val[23], b_val}))
                                      : ($signed({b_val[23], b_val}) - $signed({a_val[23], a_val}));
                out_clipped <= a_clip | b_clip;
                out_valid   <= 1'b1;
                pair_count  <= pair_count + 32'd1;
                a_have <= hit_a; b_have <= hit_b;      // a new sample of the next frame may already be here
            end else if (frame_end && (a_have ^ b_have)) begin
                err_pair_missing <= err_pair_missing + 16'd1;
                a_have <= 1'b0; b_have <= 1'b0;
            end
        end
    end
endmodule
