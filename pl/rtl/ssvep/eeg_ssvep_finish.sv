`timescale 1ns/1ps
// Finishing stage of the SSVEP scorer: turns the core results N[t,b], D[b] into one integer score per target and finds the best and the
// runner-up target.  Per (target t, band b), with e_b / W_b read from the per-model parameter memory:
//     a = |N|;  sq = a*a (128 bit);  sh = 30 - e_b;  dnum = sh >= 0 ? sq << sh : sq >> -sh;
//     Q = 0                         if D <= 0
//         0xFFFFFFFF (and QSAT)     if dnum >= D << 32
//         dnum / D (floor)          otherwise                          (32-bit quotient: ~ r^2 * 2^30 when the model is normalised)
//     S[t] += sign(N) * Q * W_b                                        (W_b unsigned Q1.15, 48-bit product)
// then best = argmax S (first maximum), second = argmax of the remaining targets (first maximum).  Everything is exact integer arithmetic;
// golden: golden/etrca_unit_golden.py (finish()).  Serial implementation: shift-add square (64 clocks) + restoring divide (96 clocks).
module eeg_ssvep_finish #(
    parameter integer NTGT = 28
)(
    input  wire clk,
    input  wire rst_n,
    input  wire start,                       // pulse: the core results of `model` are valid
    input  wire [2:0] model,
    output reg  [1:0] rd_kind,
    output reg  [8:0] rd_idx,
    input  wire [63:0] rd_data,              // one clock after rd_kind/rd_idx
    output reg  [7:0] pm_a,                  // per-model parameter memory read (combinational in the owner)
    input  wire [31:0] pm_q,
    output wire busy,
    output reg  done,                        // pulse: results below are valid
    output reg  signed [63:0] s_best,
    output reg  signed [63:0] s_second,
    output reg  [7:0] best_i,
    output reg  [7:0] second_i,
    output reg  [63:0] tr0,
    output reg  [63:0] tr4,
    output reg  q_sat,                       // some quotient saturated in this pass
    input  wire [4:0] sres_a,                // read ports of the score / quotient memories (valid one clock later)
    output reg  signed [63:0] sres_q,
    input  wire [7:0] qres_a,
    output reg  [31:0] qres_q
);
    localparam [4:0] F_IDLE = 0, F_D0 = 1, F_D1 = 2, F_D2 = 3, F_T0 = 4, F_T1 = 5, F_T2 = 6, F_P0 = 7, F_P1 = 8, F_P2 = 9, F_SQ = 10,
                     F_SH = 11, F_CK = 12, F_DIV = 13, F_QS = 14, F_MUL = 15, F_ACC = 16, F_TE = 17, F_FIN = 18;
    reg [4:0] fs;
    reg [2:0] b;
    reg [4:0] t;
    reg [2:0] m;
    reg [63:0] Dq [0:4];
    reg signed [63:0] n_q;
    reg [127:0] sq_acc, sq_m;
    reg [63:0] sq_mult;
    reg [6:0] sq_i;
    reg [159:0] dnum;
    reg [64:0] rem;
    reg [95:0] dn_sh;
    reg [95:0] qd;
    reg [6:0] dv_i;
    reg [31:0] Q;
    reg [47:0] term;
    reg signed [63:0] S_t;
    reg [9:0] e_b;
    reg [15:0] w_b;
    (* ram_style = "distributed" *) reg signed [63:0] sres [0:NTGT-1];
    (* ram_style = "distributed" *) reg [31:0] qres [0:NTGT*5-1];
    reg sres_we, qres_we;
    reg signed [63:0] sres_d;
    reg [4:0] sres_wa;
    reg [7:0] qres_wa;
    always @(posedge clk) begin
        if (sres_we) sres[sres_wa] <= sres_d;
        if (qres_we) qres[qres_wa] <= Q;
        sres_q <= sres[sres_a];
        qres_q <= qres[qres_a];
    end

    assign busy = (fs != F_IDLE);
    wire signed [9:0] sh_s = 10'sd30 - $signed(e_b);
    wire [6:0] sh_l = sh_s[9] ? 7'd0 : sh_s[6:0];
    wire [6:0] sh_r = sh_s[9] ? (-sh_s[6:0]) : 7'd0;
    wire [159:0] dn_sl = {32'b0, sq_acc} << sh_l;
    wire [159:0] dn_sr = {32'b0, sq_acc} >> sh_r;
    wire [63:0] D_cur = Dq[b];
    wire [95:0] d_shift32 = {D_cur, 32'b0};
    wire sat_chk = (dnum[159:96] != 64'd0) || (dnum[95:0] >= d_shift32);
    wire [65:0] rem_n = {rem, dn_sh[95]};
    wire [65:0] d66 = {2'b0, D_cur};
    wire rem_ge = (rem_n >= d66);
    wire [4:0] t_idx = t;
    wire [8:0] g_idx = t * 5 + b;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            fs <= F_IDLE; done <= 1'b0; sres_we <= 1'b0; qres_we <= 1'b0; q_sat <= 1'b0;
        end else begin
            done <= 1'b0; sres_we <= 1'b0; qres_we <= 1'b0;
            case (fs)
            F_IDLE: if (start) begin m <= model; b <= 3'd0; fs <= F_D0; q_sat <= 1'b0; end
            // ---- D[0..4]
            F_D0: begin rd_kind <= 2'd1; rd_idx <= {6'd0, b}; fs <= F_D1; end
            F_D1: fs <= F_D2;
            F_D2: begin
                Dq[b] <= rd_data;
                if (b == 3'd4) begin b <= 3'd0; fs <= F_T0; end else begin b <= b + 3'd1; fs <= F_D0; end
            end
            // ---- TR[0] and TR[4]
            F_T0: begin rd_kind <= 2'd2; rd_idx <= (b == 3'd0) ? 9'd0 : 9'd4; fs <= F_T1; end
            F_T1: fs <= F_T2;
            F_T2: begin
                if (b == 3'd0) begin tr0 <= rd_data; b <= 3'd4; fs <= F_T0; end
                else begin tr4 <= rd_data; b <= 3'd0; t <= 5'd0; S_t <= 64'sd0; fs <= F_P0; end
            end
            // ---- one (t, b) pair
            F_P0: begin
                rd_kind <= 2'd0; rd_idx <= g_idx;
                pm_a <= {m, 5'd7} + {5'd0, b};
                fs <= F_P1;
            end
            F_P1: begin e_b <= {3'd0, pm_q[6:0]}; pm_a <= {m, 5'd12} + {5'd0, b}; fs <= F_P2; end
            F_P2: begin
                w_b <= pm_q[15:0];
                n_q <= rd_data;
                sq_mult <= rd_data[63] ? -rd_data : rd_data;
                sq_m <= {64'd0, rd_data[63] ? -rd_data : rd_data};
                sq_acc <= 128'd0; sq_i <= 7'd0; fs <= F_SQ;
            end
            F_SQ: begin
                if (sq_mult[0]) sq_acc <= sq_acc + sq_m;
                sq_m <= sq_m << 1; sq_mult <= sq_mult >> 1; sq_i <= sq_i + 7'd1;
                if (sq_i == 7'd63) fs <= F_SH;
            end
            F_SH: begin
                dnum <= sh_s[9] ? dn_sr : dn_sl;
                fs <= F_CK;
            end
            F_CK: begin
                if (D_cur[63] || D_cur == 64'd0) begin Q <= 32'd0; fs <= F_MUL; end
                else if (sat_chk) begin Q <= 32'hffffffff; q_sat <= 1'b1; fs <= F_MUL; end
                else begin dn_sh <= dnum[95:0]; rem <= 65'd0; qd <= 96'd0; dv_i <= 7'd0; fs <= F_DIV; end
            end
            F_DIV: begin
                if (rem_ge) begin rem <= rem_n[64:0] - d66[64:0]; qd <= {qd[94:0], 1'b1}; end
                else begin rem <= rem_n[64:0]; qd <= {qd[94:0], 1'b0}; end
                dn_sh <= dn_sh << 1;
                dv_i <= dv_i + 7'd1;
                if (dv_i == 7'd95) fs <= F_QS;
            end
            F_QS: begin Q <= qd[31:0]; fs <= F_MUL; end
            F_MUL: begin
                term <= Q * w_b;
                qres_wa <= g_idx[7:0]; qres_we <= 1'b1;
                fs <= F_ACC;
            end
            F_ACC: begin
                S_t <= n_q[63] ? S_t - $signed({16'd0, term}) : S_t + $signed({16'd0, term});
                if (b == 3'd4) fs <= F_TE; else begin b <= b + 3'd1; fs <= F_P0; end
            end
            F_TE: begin
                sres_wa <= t; sres_d <= S_t; sres_we <= 1'b1;
                if (t == 5'd0) begin s_best <= S_t; best_i <= 8'd0; s_second <= -64'sd36028797018963968; second_i <= 8'd0; end
                else if (S_t > s_best) begin s_second <= s_best; second_i <= best_i; s_best <= S_t; best_i <= {3'd0, t}; end
                else if (S_t > s_second) begin s_second <= S_t; second_i <= {3'd0, t}; end
                if (t == NTGT - 1) fs <= F_FIN;
                else begin t <= t + 5'd1; b <= 3'd0; S_t <= 64'sd0; fs <= F_P0; end
            end
            F_FIN: begin done <= 1'b1; fs <= F_IDLE; end
            default: fs <= F_IDLE;
            endcase
        end
    end
endmodule
