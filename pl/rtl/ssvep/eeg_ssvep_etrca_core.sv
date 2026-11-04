`timescale 1ns/1ps
// Calibrated SSVEP scorer core (Fourier-template ensemble TRCA), accumulation stage.  Golden model: golden/etrca_unit_golden.py (bit exact).
//
// One frame = 5 sub-bands x 8 channels of signed 16-bit samples (in_idx = band*8+channel, 40 words, in_idx==39 closes the frame).  For every
// target t and harmonic h the core generates sin/cos of phase n*h*FCW[t] (mod 2^32, 12-bit table index) from a 4096 x Q1.15 table and
// accumulates P[t,b,c,k] (48 bit) and the 8x8 data covariance C[b] (48 bit).  `score_req` then forms, with signed 64-bit results,
//   N[t,b] = sum_{c,k} G[m,t,b,c,k] P[t,b,c,k]      D[b] = sum_{i,j} M[m,b,i,j] C[b,i,j]      TR[b] = trace C[b]
// for the model m = model_sel.  The first frame after `start_now` (n = 0) reads P, C and the running phases as zero ("fresh"), so no
// clearing pass is needed.  Arithmetic is deliberately serial (one multiplier, 7 clocks per product): a frame takes about
// 7*(NTGT*NH*2*40 + 320) clocks and a scoring pass 7*(NTGT*5*80 + 320), both far below the 409,600 clocks between frames.
//
// Parameter port (prm_we/prm_addr/prm_data, 18-bit word address): FCW at 0x00000+t (u32, phase increment per sample = f/fs*2^32),
//   M[m][b][i][j] at 0x10000 + m*320 + (b*8+i)*8+j (s16), G[m][t][b][c][k] at 0x20000 + m*GDEPTH + ((t*5+b)*8+c)*K + k (s16, 17-bit index),
//   K = 2*NH (k = 2(h-1) sin, 2(h-1)+1 cos), GDEPTH = NTGT*40*K.
module eeg_ssvep_etrca_core #(
    parameter integer NTGT = 28,
    parameter integer NH = 5,
    parameter integer NMODEL = 8
)(
    input  wire clk,
    input  wire rst_n,
    input  wire start_now,                   // idle only: the next frame is sample n = 0 of a new window
    input  wire prm_we,
    input  wire [17:0] prm_addr,
    input  wire [31:0] prm_data,
    input  wire in_valid,
    input  wire [5:0] in_idx,
    input  wire signed [15:0] in_x,
    input  wire score_req,                   // idle only
    input  wire [2:0] model_sel,
    output wire ready,                       // idle: may take start_now / a frame / score_req
    output reg  frame_done,                  // one-clock pulse after a frame was accumulated (nsamp already updated)
    output reg  score_done,                  // one-clock pulse, results valid
    output reg  [15:0] nsamp,                // frames accumulated since start_now
    output reg  error_sticky,                // start/frame/score request while busy
    input  wire [1:0] rd_kind,               // 0 N[idx] 1 D[idx] 2 TR[idx]
    input  wire [8:0] rd_idx,
    output reg  [63:0] rd_data               // one clock after rd_kind/rd_idx
);
    localparam integer K = 2 * NH;
    localparam integer GDEPTH = NTGT * 40 * K;
    localparam integer NGRP = NTGT * 5;

    // ------------------------------------------------------------------ memories
    (* ram_style = "block" *) reg [47:0] pram [0:GDEPTH-1];
    (* ram_style = "block" *) reg [15:0] gram [0:GDEPTH*NMODEL-1];
    (* ram_style = "distributed" *) reg [15:0] mram [0:320*NMODEL-1];
    (* ram_style = "distributed" *) reg [47:0] cram [0:319];
    (* ram_style = "distributed" *) reg [31:0] fcw  [0:NTGT-1];
    (* ram_style = "distributed" *) reg [31:0] phram [0:NTGT*NH-1];
    (* ram_style = "distributed" *) reg [63:0] nres [0:NGRP-1];
    reg [63:0] dres [0:4];
    reg [63:0] trres [0:4];
    reg nres_we, dres_we;
    reg [7:0] nres_a;
    reg [2:0] dres_a;
    reg [63:0] res_d, res_tr;
    always @(posedge clk) begin
        if (nres_we) nres[nres_a] <= res_d;
        if (dres_we) begin dres[dres_a] <= res_d; trres[dres_a] <= res_tr; end
    end
    always @(posedge clk) begin
        case (rd_kind)
            2'd0:    rd_data <= nres[rd_idx[7:0]];
            2'd1:    rd_data <= dres[rd_idx[2:0]];
            default: rd_data <= trres[rd_idx[2:0]];
        endcase
    end

    reg [15:0] xreg [0:39];

    // parameter writes (independent of the engine; the PS writes only while idle)
    always @(posedge clk) begin
        if (prm_we) begin
            if (prm_addr[17]) gram[prm_addr[16:0]] <= prm_data[15:0];
            else if (prm_addr[16]) mram[prm_addr[11:0]] <= prm_data[15:0];
            else if (prm_addr[15:8] == 8'd0 && prm_addr[7:0] < NTGT) fcw[prm_addr[7:0]] <= prm_data;
        end
    end

    // ------------------------------------------------------------------ sample capture
    reg frame_go;
    always @(posedge clk) if (in_valid) xreg[in_idx] <= in_x;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) frame_go <= 1'b0;
        else frame_go <= in_valid && (in_idx == 6'd39);
    end

    // ------------------------------------------------------------------ engine
    localparam [3:0] S_IDLE = 0, S_PH = 2, S_PE = 3, S_PHW = 4, S_CE = 5, S_SD = 6, S_SN = 7, S_SDN = 8;
    reg [3:0] state;
    reg [3:0] u;
    reg [4:0] t;
    reg [2:0] h;
    reg kk;
    reg [5:0] bc;
    reg [2:0] cb;                        // band for the covariance / D loops
    reg [5:0] ce;                        // element inside one band (0..63)
    reg [7:0] grp;                       // (t*5+b) group for N
    reg [6:0] gj;                        // 0..79 inside a group
    reg fresh;
    reg [2:0] model_q;

    reg [31:0] ph, inc;
    reg signed [15:0] sin_v, cos_v, ref_v;
    reg [11:0] rom_a;
    wire [15:0] rom_q;
    eeg_ssvep_sine_rom u_sin(.clk(clk), .addr(rom_a), .q(rom_q));
    reg [31:0] fcw_q, ph_q;

    reg [13:0] paddr;
    reg [47:0] pq, pwd;
    reg pwe;
    reg [47:0] cq, cwd;
    reg [8:0] caddr;
    reg cwe;

    reg signed [15:0] xa, xb;
    reg signed [31:0] prod32;

    // wide multiply: op16 (signed 16) x op48 (signed 48) -> signed 64, three registered stages
    reg signed [15:0] op16;
    reg signed [47:0] op48;
    reg signed [40:0] m_lo;              // op16 x {0, low 24}
    reg signed [39:0] m_hi;              // op16 x high 24 (signed)
    reg signed [63:0] prod64;
    reg signed [63:0] acc64, tr_acc;
    reg diag_q;

    assign ready = (state == S_IDLE);

    // synchronous memory read ports (one address register each)
    reg [16:0] pr_a;                     // gram read address (model offset included)
    reg [11:0] mr_a;
    reg [7:0] fr_a;
    reg [7:0] pha;
    reg [47:0] pram_q, cram_q;
    reg [15:0] gram_q, mram_q;
    reg [31:0] fcw_o, ph_o;
    always @(posedge clk) begin
        if (pwe) pram[paddr] <= pwd;
        pram_q <= pram[paddr];
        gram_q <= gram[pr_a];
        if (cwe) cram[caddr] <= cwd;
        cram_q <= cram[caddr];
        mram_q <= mram[mr_a];
        fcw_o <= fcw[fr_a];
        ph_o <= phram[pha];
    end

    // phase RAM written by the engine (separate always block keeps one writer per array)
    reg phwe;
    reg [7:0] pha_w;
    reg [31:0] phwd;
    always @(posedge clk) if (phwe) phram[pha_w] <= phwd;

    wire [13:0] gi_pe = t * (40 * K) + bc * K + (2 * h + kk);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE; u <= 0; pwe <= 0; cwe <= 0; phwe <= 0; score_done <= 0; frame_done <= 0; nsamp <= 0; error_sticky <= 0;
            nres_we <= 0; dres_we <= 0; fresh <= 1'b1; model_q <= 0;
        end else begin
            pwe <= 1'b0; cwe <= 1'b0; phwe <= 1'b0; score_done <= 1'b0; frame_done <= 1'b0; nres_we <= 1'b0; dres_we <= 1'b0;
            if (state != S_IDLE && (start_now || frame_go || score_req)) error_sticky <= 1'b1;
            case (state)
            S_IDLE: begin
                u <= 0;
                if (start_now) begin fresh <= 1'b1; nsamp <= 0; end
                else if (frame_go) begin state <= S_PH; t <= 0; h <= 0; u <= 0; end
                else if (score_req) begin state <= S_SD; cb <= 0; ce <= 0; u <= 0; acc64 <= 0; tr_acc <= 0; model_q <= model_sel; end
            end
            // ---- per (t,h): fetch phase and FCW, form sin/cos ---------------------------------------------------------
            S_PH: begin
                case (u)
                    0: begin fr_a <= t; pha <= t * NH + h; u <= 1; end
                    1: u <= 2;
                    2: begin fcw_q <= fcw_o; ph_q <= fresh ? 32'd0 : ph_o; u <= 3; end
                    3: begin ph <= ph_q; inc <= fcw_q * (h + 1); rom_a <= ph_q[31:20]; u <= 4; end
                    4: u <= 5;
                    5: begin sin_v <= rom_q; rom_a <= ph[31:20] + 12'd1024; u <= 6; end
                    6: u <= 7;
                    7: begin cos_v <= rom_q; kk <= 1'b0; bc <= 0; u <= 0; state <= S_PE; end
                    default: u <= 0;
                endcase
            end
            // ---- one P element ---------------------------------------------------------------------------------------
            S_PE: begin
                case (u)
                    0: begin paddr <= gi_pe; u <= 1; end
                    1: u <= 2;
                    2: begin pq <= fresh ? 48'd0 : pram_q; xa <= xreg[bc]; ref_v <= kk ? cos_v : sin_v; u <= 3; end
                    3: begin prod32 <= xa * ref_v; u <= 4; end
                    4: begin pwd <= pq + {{16{prod32[31]}}, prod32}; u <= 5; end
                    5: begin pwe <= 1'b1; u <= 6; end
                    6: begin
                        u <= 0;
                        if (bc == 39) begin
                            bc <= 0;
                            if (!kk) kk <= 1'b1;
                            else begin state <= S_PHW; end
                        end else bc <= bc + 1'b1;
                    end
                    default: u <= 0;
                endcase
            end
            S_PHW: begin
                pha_w <= t * NH + h; phwd <= ph + inc; phwe <= 1'b1; u <= 0;
                if (h == NH - 1) begin
                    h <= 0;
                    if (t == NTGT - 1) begin state <= S_CE; cb <= 0; ce <= 0; end
                    else begin t <= t + 1'b1; state <= S_PH; end
                end else begin h <= h + 1'b1; state <= S_PH; end
            end
            // ---- covariance element (full 8x8 per band) ---------------------------------------------------------------
            S_CE: begin
                case (u)
                    0: begin caddr <= cb * 64 + ce; u <= 1; end
                    1: u <= 2;
                    2: begin cq <= fresh ? 48'd0 : cram_q; xa <= xreg[cb * 8 + ce[5:3]]; xb <= xreg[cb * 8 + ce[2:0]]; u <= 3; end
                    3: begin prod32 <= xa * xb; u <= 4; end
                    4: begin cwd <= cq + {{16{prod32[31]}}, prod32}; u <= 5; end
                    5: begin cwe <= 1'b1; u <= 6; end
                    6: begin
                        u <= 0;
                        if (ce == 63) begin
                            ce <= 0;
                            if (cb == 4) begin
                                state <= S_IDLE; nsamp <= nsamp + 1'b1; fresh <= 1'b0; frame_done <= 1'b1;
                            end else cb <= cb + 1'b1;
                        end else ce <= ce + 1'b1;
                    end
                    default: u <= 0;
                endcase
            end
            // ---- D[b] = sum M*C and TR[b] = trace C --------------------------------------------------------------------
            S_SD: begin
                case (u)
                    0: begin mr_a <= model_q * 320 + cb * 64 + ce; caddr <= cb * 64 + ce; u <= 1; end
                    1: u <= 2;
                    2: begin op16 <= mram_q; op48 <= cram_q; diag_q <= (ce[5:3] == ce[2:0]); u <= 3; end
                    3: begin m_lo <= op16 * $signed({1'b0, op48[23:0]}); m_hi <= op16 * $signed(op48[47:24]); u <= 4; end
                    4: begin prod64 <= ($signed({{24{m_hi[39]}}, m_hi}) <<< 24) + $signed({{23{m_lo[40]}}, m_lo}); u <= 5; end
                    5: begin
                        acc64 <= acc64 + prod64;
                        if (diag_q) tr_acc <= tr_acc + {{16{op48[47]}}, op48};
                        u <= 6;
                    end
                    6: begin
                        u <= 0;
                        if (ce == 63) begin
                            dres_a <= cb; res_d <= acc64; res_tr <= tr_acc; dres_we <= 1'b1;
                            ce <= 0; acc64 <= 0; tr_acc <= 0;
                            if (cb == 4) begin state <= S_SN; grp <= 0; gj <= 0; end else cb <= cb + 1'b1;
                        end else ce <= ce + 1'b1;
                    end
                    default: u <= 0;
                endcase
            end
            // ---- N[g] = sum G*P over 80 consecutive entries ------------------------------------------------------------
            S_SN: begin
                case (u)
                    0: begin pr_a <= model_q * GDEPTH + grp * 80 + gj; paddr <= grp * 80 + gj; u <= 1; end
                    1: u <= 2;
                    2: begin op16 <= gram_q; op48 <= pram_q; u <= 3; end
                    3: begin m_lo <= op16 * $signed({1'b0, op48[23:0]}); m_hi <= op16 * $signed(op48[47:24]); u <= 4; end
                    4: begin prod64 <= ($signed({{24{m_hi[39]}}, m_hi}) <<< 24) + $signed({{23{m_lo[40]}}, m_lo}); u <= 5; end
                    5: begin acc64 <= acc64 + prod64; u <= 6; end
                    6: begin
                        u <= 0;
                        if (gj == 79) begin
                            nres_a <= grp; res_d <= acc64; nres_we <= 1'b1;
                            gj <= 0; acc64 <= 0;
                            if (grp == NGRP - 1) begin state <= S_SDN; end else grp <= grp + 1'b1;
                        end else gj <= gj + 1'b1;
                    end
                    default: u <= 0;
                endcase
            end
            S_SDN: begin score_done <= 1'b1; state <= S_IDLE; end
            default: state <= S_IDLE;
            endcase
        end
    end
endmodule
