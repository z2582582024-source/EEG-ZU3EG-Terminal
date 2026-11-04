`timescale 1ns/1ps
// 5-band causal IIR filter bank of the SSVEP scorer (the product FBCCA bank: 50 Hz notch + five 4th-order Butterworth band-passes, Q30
// biquads, 24-bit state-exact arithmetic identical to the golden Bq24): one frame = 8 posterior channels at 244.140625 SPS in (signed 24 bit,
// the decimator output), 40 sub-band samples out (index = band*8 + channel, signed 16 bit after the programmable right shift `xshift`
// with round-half-away-from-zero and saturation).  The sections are evaluated serially with one pipelined 34x24 multiplier, ~17 clocks
// per section, 168 sections per frame (~3k clocks of the 409,600 available between frames).
//   golden: golden/etrca_unit_golden.py (class Fbank)
module eeg_ssvep_mul34x24 (
    input  wire clk,
    input  wire signed [33:0] a,
    input  wire signed [23:0] b,
    output reg  signed [57:0] p              // valid 3 clocks after a/b were presented
);
    reg signed [17:0] ah;
    reg [15:0] al;
    reg signed [23:0] bq;
    reg signed [41:0] mh;
    reg signed [40:0] ml;
    always @(posedge clk) begin
        ah <= a[33:16];
        al <= a[15:0];
        bq <= b;
        mh <= bq * ah;                                    // 24 x 18 signed
        ml <= bq * $signed({1'b0, al});                   // 24 x 16 unsigned
        p  <= ($signed({{16{mh[41]}}, mh}) <<< 16) + $signed({{17{ml[40]}}, ml});
    end
endmodule

module eeg_ssvep_fbank (
    input  wire clk,
    input  wire rst_n,
    input  wire in_valid,                    // one pulse per posterior channel sample
    input  wire [2:0] in_ch,
    input  wire signed [23:0] in_x,
    input  wire in_sat,                      // upstream saturation of this sample (decimator)
    input  wire in_last,                     // this is the last channel of the frame: processing starts after it
    input  wire [31:0] in_seq,               // decimated sequence number of the frame (latched with in_last)
    input  wire [3:0] xshift,
    input  wire clr,                         // pulse (idle only): zero all filter states
    output wire busy,
    output reg  drop,                        // pulse: a frame arrived while busy (dropped)
    output reg  out_valid,
    output reg  [5:0] out_idx,
    output reg  signed [15:0] out_x,
    output reg  out_last,
    output reg  [31:0] out_seq,
    output reg  out_clip                     // valid with out_last: any saturation inside this frame
);
    // ---------------------------------------------------------------- input capture
    reg signed [23:0] xbuf [0:7];
    reg [31:0] seq_q;
    reg frame_clip;
    reg go;
    always @(posedge clk) if (in_valid) xbuf[in_ch] <= in_x;

    // ---------------------------------------------------------------- engine
    localparam [2:0] S_IDLE = 0, S_SEC = 1, S_NEXT = 2, S_EMIT = 3, S_CLR = 4;
    reg [2:0] state;
    reg [4:0] u;
    reg [2:0] c;
    reg [4:0] sec;
    reg [7:0] saddr;
    reg signed [23:0] sec_in, y0, y_r;
    reg signed [23:0] xo [0:39];
    reg [127:0] st_q;
    reg [127:0] st_ram [0:167];
    reg [7:0] st_ra;
    reg st_we;
    reg [7:0] st_wa;
    reg [127:0] st_wd;
    wire signed [33:0] b0, b1, b2, a1, a2;
    eeg_ssvep_fbank_rom u_rom(.clk(clk), .addr(sec), .b0(b0), .b1(b1), .b2(b2), .a1(a1), .a2(a2));

    reg signed [33:0] mul_a;
    wire signed [57:0] mul_p;
    eeg_ssvep_mul34x24 u_mul(.clk(clk), .a(mul_a), .b(u <= 5'd4 ? sec_in : y_r), .p(mul_p));
    wire signed [63:0] p_ext = {{6{mul_p[57]}}, mul_p};

    reg signed [63:0] z1, z2, z1n, z2n, p1_r, p2_r, acc_r, abs_r, sh_r, rnd_r;
    reg acc_neg, sec_clip;
    localparam signed [63:0] HALF = 64'sd536870912;           // 2^29
    localparam signed [63:0] Q_MAX = 64'sd8388607, Q_MIN = -64'sd8388608;

    integer ini;
    initial begin
        for (ini = 0; ini < 168; ini = ini + 1) st_ram[ini] = 128'd0;
    end
    always @(posedge clk) begin
        if (st_we) st_ram[st_wa] <= st_wd;
        st_q <= st_ram[st_ra];
    end

    always @(*) begin
        case (u)
            5'd2:  mul_a = b0;
            5'd3:  mul_a = b1;
            5'd4:  mul_a = b2;
            5'd10: mul_a = a1;
            5'd11: mul_a = a2;
            default: mul_a = 34'sd0;
        endcase
    end

    wire [2:0] band_of_sec = (sec - 5'd1) >> 2;
    wire [1:0] j_of_sec = (sec - 5'd1);

    // emission pipeline registers
    reg [5:0] em_i;
    reg em_v1, em_v2, em_v3;
    reg [5:0] em_i1, em_i2, em_i3;
    reg signed [23:0] em_x1;
    reg [23:0] em_abs;
    reg em_neg, em_neg3;
    reg [24:0] em_sh, em_sh3;
    reg em_clip_acc;
    wire [23:0] em_half = (xshift == 4'd0) ? 24'd0 : (24'd1 << (xshift - 4'd1));
    wire signed [25:0] em_signed = em_neg3 ? -$signed({1'b0, em_sh3}) : $signed({1'b0, em_sh3});
    wire em_sat = (em_signed > 26'sd32767) || (em_signed < -26'sd32768);
    wire signed [15:0] em_sat_val = (em_signed > 26'sd32767) ? 16'sh7fff : (em_signed < -26'sd32768) ? -16'sh8000 : em_signed[15:0];

    assign busy = (state != S_IDLE);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE; u <= 0; c <= 0; sec <= 0; saddr <= 0; st_we <= 0; go <= 0;
            out_valid <= 0; out_last <= 0; out_clip <= 0; drop <= 0; frame_clip <= 0; em_v1 <= 0; em_v2 <= 0;
            out_idx <= 0; out_x <= 0; out_seq <= 0; seq_q <= 0; em_i <= 0; em_clip_acc <= 0;
        end else begin
            st_we <= 1'b0;
            out_valid <= 1'b0; out_last <= 1'b0; drop <= 1'b0;
            em_v1 <= 1'b0; em_v2 <= 1'b0;
            if (in_valid && in_sat) frame_clip <= 1'b1;
            if (in_valid && (in_x == 24'sh7fffff || in_x == -24'sh800000)) frame_clip <= 1'b1;
            case (state)
            S_IDLE: begin
                u <= 0;
                if (clr) begin state <= S_CLR; saddr <= 8'd0; end
                else if (in_valid && in_last) begin
                    seq_q <= in_seq;
                    c <= 0; sec <= 0; saddr <= 0; state <= S_SEC; u <= 0;
                end
            end
            S_SEC: begin
                if (in_valid && in_last) drop <= 1'b1;
                case (u)
                5'd0: begin
                    st_ra <= saddr;
                    if (sec == 5'd0) sec_in <= xbuf[c];
                    else if (j_of_sec == 2'd0) sec_in <= y0;
                    else sec_in <= y_r;
                    u <= 5'd1;
                end
                5'd1: u <= 5'd2;
                5'd2: begin z1 <= st_q[63:0]; z2 <= st_q[127:64]; u <= 5'd3; end
                5'd3: u <= 5'd4;
                5'd4: u <= 5'd5;
                5'd5: begin acc_r <= p_ext + z1; u <= 5'd6; end
                5'd6: begin p1_r <= p_ext; acc_neg <= acc_r[63]; abs_r <= acc_r[63] ? -acc_r : acc_r; u <= 5'd7; end
                5'd7: begin p2_r <= p_ext; sh_r <= (abs_r + HALF) >>> 30; u <= 5'd8; end
                5'd8: begin rnd_r <= acc_neg ? -sh_r : sh_r; u <= 5'd9; end
                5'd9: begin
                    y_r <= (rnd_r > Q_MAX) ? 24'sh7fffff : (rnd_r < Q_MIN) ? -24'sh800000 : rnd_r[23:0];
                    sec_clip <= (rnd_r > Q_MAX) || (rnd_r < Q_MIN);
                    u <= 5'd10;
                end
                5'd10: u <= 5'd11;
                5'd11: begin if (sec_clip) frame_clip <= 1'b1; u <= 5'd12; end
                5'd12: u <= 5'd13;
                5'd13: begin z1n <= p1_r - p_ext + z2; u <= 5'd14; end
                5'd14: begin z2n <= p2_r - p_ext; u <= 5'd15; end
                5'd15: begin st_we <= 1'b1; st_wa <= saddr; st_wd <= {z2n, z1n}; u <= 5'd16; end
                5'd16: begin
                    // section finished: route its output
                    if (sec == 5'd0) y0 <= y_r;
                    if (sec != 5'd0 && j_of_sec == 2'd3) xo[{band_of_sec, c}] <= y_r;
                    u <= 5'd0;
                    saddr <= saddr + 8'd1;
                    if (sec == 5'd20) begin
                        sec <= 5'd0;
                        if (c == 3'd7) begin state <= S_EMIT; em_i <= 6'd0; end
                        else c <= c + 3'd1;
                    end else sec <= sec + 5'd1;
                end
                default: u <= 5'd0;
                endcase
            end
            S_CLR: begin
                st_we <= 1'b1; st_wa <= saddr; st_wd <= 128'd0;
                saddr <= saddr + 8'd1;
                if (saddr == 8'd167) state <= S_IDLE;
            end
            S_EMIT: begin
                // 40 elements, one per clock into a 3-stage pipeline: read -> abs/round -> saturate
                if (em_i < 6'd40) begin
                    em_x1 <= xo[em_i[5:0]]; em_i1 <= em_i; em_v1 <= 1'b1; em_i <= em_i + 6'd1;
                end
                if (em_v1) begin
                    em_neg <= em_x1[23];
                    em_abs <= em_x1[23] ? -em_x1 : em_x1;
                    em_i2 <= em_i1; em_v2 <= 1'b1;
                end
                // stage 3 is below (needs em_v3)
                if (em_v3) begin
                    out_valid <= 1'b1; out_idx <= em_i3;
                    out_x <= em_sat_val;
                    if (em_sat) frame_clip <= 1'b1;
                    if (em_i3 == 6'd39) begin
                        out_last <= 1'b1; out_seq <= seq_q; out_clip <= frame_clip | em_sat;
                        state <= S_IDLE; frame_clip <= 1'b0;
                    end
                end
            end
            default: state <= S_IDLE;
            endcase
        end
    end

    // third emission stage
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin em_v3 <= 0; em_i3 <= 0; em_neg3 <= 0; em_sh3 <= 0; end
        else begin
            em_v3 <= em_v2; em_i3 <= em_i2; em_neg3 <= em_neg; em_sh3 <= ({1'b0, em_abs} + {1'b0, em_half}) >> xshift;
        end
    end
endmodule
