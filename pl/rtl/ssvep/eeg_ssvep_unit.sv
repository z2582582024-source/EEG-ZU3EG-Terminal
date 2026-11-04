`timescale 1ns/1ps
// SSVEP scoring unit: PL filter bank -> eTRCA accumulation core -> division / argmax finishing -> gated multi-checkpoint decision ->
// CRC-protected decision records, with a register window, hardware latency metering and a capture RAM for calibration.
// Golden model: golden/etrca_unit_golden.py (bit exact for the filter bank, the scores, the decisions and the record words).
//
// Data path:  tap (decimator output, ch 8..15, 244.140625 SPS) or software FEED  ->  eeg_ssvep_fbank (40 sub-band samples / frame)
//             -> eeg_ssvep_etrca_core (P, C accumulation; N, D, TR at checkpoints)  ->  eeg_ssvep_finish (score per target, best/second)
//             -> gate (score, margin, quality) + agreement counter  ->  record FIFO (16 x 16 words, CRC32)
//
// Window alignment: the PS writes ARM_SEQ (decimated sequence number of sample n = 0) and CTRL.ARM; the window starts exactly at the frame whose
// sequence equals ARM_SEQ (a frame already past it sets ARM_MISSED).  Checkpoints: model m is scored when nsamp == CKPT[m].
//
// Register window (byte offsets 0x200 + 4*i, 32-bit, seen through the AXI-Lite boundary adapter):
//   0 ID 'ETR2' | 1 CTRL w (0 ARM,1 ABORT,2 SCORE,3 CLR_ERR,4 CLR_HIST,5 CLR_FIFO,6 CLR_PCRC,7 CAP_RESET,8 FB_CLR) | 2 STATUS | 3 NSAMP | 4 ARM_SEQ
//   5 CUR_SEQ | 6 CFG | 7 PADDR | 8 PDATA (auto-increment) | 9 FEED | 10 RECD (pop) | 11 REC_STATUS | 12 CYC_LO | 13 CYC_HI (latched by CYC_LO)
//   14 QMASK | 15 CAP_ADDR | 16 CAP_DATA (auto-increment) | 17 CAP_LEN | 18 CAP_MAX | 19..23 checkpoint latency min/max/sum_lo/sum_hi/count
//   24..28 frame latency min/max/sum_lo/sum_hi/count | 29 PCRC | 30 ERR | 31 CAL_ID | 32 INFO
// CFG: [3:0] xshift  [7:4] agree  [8] feed_mode  [9] decision_enable  [10] cap_en  [11] tap_enable  [18:16] manual model  [22:20] final model
// FEED word: [23:0] sample (S24), [26:24] channel 0..7 (channel 7 closes the frame), [28] saturation flag.
// Pointer spaces (PADDR/PDATA, 18-bit word address):
//   write  0x00000+t FCW[t] | 0x00100 + m*32 + i per-model parameter i | 0x10000 + m*320 + (b*8+i)*8+j M | 0x20000 + m*GDEPTH + ((t*5+b)*8+c)*K + k G
//   read   0x00100.. parameters | 0x03000 N (lo/hi words) | 0x04000 D | 0x05000 TR | 0x06000 S_t | 0x07000 Q | 0x08000 hist(ckpt latency)
//          0x09000 hist(frame latency) | 0x0A000 info
// Per-model parameters (word i of model m at 0x100 + m*32 + i): 0 CKPT, 1 THR_S lo, 2 THR_S hi(24), 3 THR_M(Q0.16), 4 Q_FLAT, 5 Q_HUGE,
//   6 EMG_THR(Q8.8 ratio of band-4 to band-0 power), 7..11 E_b (shift: Q = N^2 * 2^(30-E_b) / D), 12..16 W_b (unsigned Q1.15 band weight).
module eeg_crc32w (
    input  wire [31:0] c,
    input  wire [31:0] w,
    output reg  [31:0] n
);
    integer k;
    reg [31:0] v;
    always @(*) begin
        v = c ^ w;
        for (k = 0; k < 32; k = k + 1) v = (v >> 1) ^ (32'hedb88320 & (32'd0 - (v & 32'd1)));
        n = v;
    end
endmodule

module eeg_ssvep_unit #(
    parameter integer NTGT = 28,
    parameter integer NH = 5,
    parameter integer NMODEL = 8,
    parameter integer CAP_FRAMES = 1024
)(
    input  wire clk,
    input  wire rst_n,
    // live tap: decimator output of the bridge
    input  wire tap_valid,
    input  wire [3:0] tap_ch,
    input  wire signed [23:0] tap_x,
    input  wire tap_sat,
    input  wire [31:0] tap_seq,
    // register window (from the AXI-Lite boundary adapter)
    input  wire x_wen,
    input  wire [11:0] x_waddr,
    input  wire [31:0] x_wdata,
    output reg  x_wack,
    output reg  x_werr,
    input  wire x_ren,
    input  wire [11:0] x_raddr,
    output reg  x_rvalid,
    output reg  [31:0] x_rdata,
    output reg  x_rerr,
    output reg  decision_pulse
);
    localparam integer GDEPTH = NTGT * 40 * 2 * NH;
    localparam integer CAP_WORDS = CAP_FRAMES * 20;

    // ================================================================= register state (written only by the window write block)
    reg [31:0] cfg, arm_seq, qmask, cal_id, paddr;
    reg [15:0] cap_max;
    reg [8:0] ctrl_r;
    reg feed_wr;
    reg [31:0] feed_word;
    reg prm_we_r;
    reg [17:0] prm_addr_r;
    reg [31:0] prm_data_r;
    reg [2:0] pc_st;
    reg [31:0] pc_a, pc_d, pcrc;
    reg [31:0] err_bits;
    (* ram_style = "distributed" *) reg [31:0] pmem [0:255];
    integer ini;
    initial begin
        for (ini = 0; ini < 256; ini = ini + 1) pmem[ini] = 32'd0;
    end

    wire ctrl_arm = ctrl_r[0], ctrl_abort = ctrl_r[1], ctrl_score = ctrl_r[2], ctrl_clr_err = ctrl_r[3], ctrl_clr_hist = ctrl_r[4],
         ctrl_clr_fifo = ctrl_r[5], ctrl_clr_pcrc = ctrl_r[6], ctrl_cap_reset = ctrl_r[7], ctrl_fbclr = ctrl_r[8];
    wire [3:0] xshift = cfg[3:0];
    wire [3:0] agree_n = (cfg[7:4] == 4'd0) ? 4'd1 : cfg[7:4];
    wire feed_mode = cfg[8];
    wire dec_en = cfg[9];
    wire cap_en = cfg[10];
    wire tap_en = cfg[11];
    wire [2:0] model_manual = cfg[18:16];
    wire [2:0] final_model = cfg[22:20];

    // read-side bookkeeping touched by the read pipeline
    reg [14:0] cap_rp;
    reg err_set_rdconf;
    reg rd_cap_inc, rd_paddr_inc;
    wire [31:0] pcrc_n;
    reg [7:0] last_best_r;
    reg [2:0] last_model_r;
    reg [3:0] rec_wp, rec_rp;
    reg [4:0] rec_cnt;
    reg [3:0] rec_rw;
    reg rec_overflow;
    reg pop_p;
    localparam [7:0] NM8 = NMODEL, NT8 = NTGT;

    // ================================================================= free-running cycle counter and latency bookkeeping
    reg [63:0] cyc;
    reg [31:0] cyc_hi_l;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) cyc <= 64'd0; else cyc <= cyc + 64'd1;
    end

    // ================================================================= frame source
    reg  fb_in_valid;
    reg  [2:0] fb_in_ch;
    reg  signed [23:0] fb_in_x;
    reg  fb_in_sat, fb_in_last;
    reg  [31:0] fb_in_seq;
    reg  [31:0] feed_seq, cur_seq;
    wire live_ok = tap_en && !feed_mode && tap_valid && tap_ch[3];
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            fb_in_valid <= 0; fb_in_ch <= 0; fb_in_x <= 0; fb_in_sat <= 0; fb_in_last <= 0; fb_in_seq <= 0; feed_seq <= 0; cur_seq <= 0;
        end else begin
            fb_in_valid <= 1'b0; fb_in_last <= 1'b0;
            if (live_ok) begin
                fb_in_valid <= 1'b1; fb_in_ch <= tap_ch[2:0]; fb_in_x <= tap_x; fb_in_sat <= tap_sat;
                fb_in_last <= (tap_ch == 4'd15); fb_in_seq <= tap_seq;
            end else if (feed_wr && feed_mode) begin
                fb_in_valid <= 1'b1; fb_in_ch <= feed_word[26:24]; fb_in_x <= feed_word[23:0]; fb_in_sat <= feed_word[28];
                fb_in_last <= (feed_word[26:24] == 3'd7); fb_in_seq <= feed_seq;
                if (feed_word[26:24] == 3'd7) feed_seq <= feed_seq + 32'd1;
            end
            if (ctrl_abort) feed_seq <= 32'd0;
            if (fb_in_valid && fb_in_last) cur_seq <= fb_in_seq;
        end
    end

    // ================================================================= arming / window control
    reg armed, running, decided, arm_missed, no_decision, fwd_frame, start_evt;
    reg [31:0] t_arr, win_start_seq;
    reg win_clip;
    wire win_done_p;
    wire dec_decided_p, dec_nodec_p;
    wire [31:0] seq_diff = fb_in_seq - arm_seq;
    wire frame_start = fb_in_valid && fb_in_last;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            armed <= 0; running <= 0; fwd_frame <= 0; start_evt <= 0; arm_missed <= 0; t_arr <= 0; win_start_seq <= 0;
            decided <= 0; no_decision <= 0;
        end else begin
            start_evt <= 1'b0;
            if (ctrl_arm) begin armed <= 1'b1; arm_missed <= 1'b0; decided <= 1'b0; no_decision <= 1'b0; running <= 1'b0; end
            if (ctrl_abort) begin armed <= 1'b0; running <= 1'b0; decided <= 1'b0; no_decision <= 1'b0; end
            if (frame_start) begin
                t_arr <= cyc[31:0];
                if (armed && seq_diff == 32'd0) begin
                    armed <= 1'b0; running <= 1'b1; start_evt <= 1'b1; fwd_frame <= 1'b1; win_start_seq <= fb_in_seq;
                end else if (armed && !seq_diff[31]) begin
                    armed <= 1'b0; arm_missed <= 1'b1; fwd_frame <= 1'b0;
                end else fwd_frame <= running;
            end
            if (win_done_p) running <= 1'b0;
            if (dec_decided_p) decided <= 1'b1;
            if (dec_nodec_p) no_decision <= 1'b1;
        end
    end

    // ================================================================= filter bank
    wire fb_busy, fb_drop, fb_ov, fb_olast, fb_oclip;
    wire [5:0] fb_oidx;
    wire signed [15:0] fb_ox;
    wire [31:0] fb_oseq;
    eeg_ssvep_fbank u_fbank(.clk(clk), .rst_n(rst_n), .in_valid(fb_in_valid), .in_ch(fb_in_ch), .in_x(fb_in_x), .in_sat(fb_in_sat),
        .in_last(fb_in_last), .in_seq(fb_in_seq), .xshift(xshift), .clr(ctrl_fbclr), .busy(fb_busy), .drop(fb_drop), .out_valid(fb_ov), .out_idx(fb_oidx),
        .out_x(fb_ox), .out_last(fb_olast), .out_seq(fb_oseq), .out_clip(fb_oclip));

    // ================================================================= core
    reg sc_req;
    reg [2:0] sc_model;
    wire core_ready, core_frame_done, core_score_done, core_err;
    wire [15:0] core_nsamp;
    wire [1:0] f_rd_kind;
    wire [8:0] f_rd_idx;
    reg  [1:0] ps_rd_kind;
    reg  [8:0] ps_rd_idx;
    wire fin_busy;
    wire [1:0] rd_kind = fin_busy ? f_rd_kind : ps_rd_kind;
    wire [8:0] rd_idx = fin_busy ? f_rd_idx : ps_rd_idx;
    wire [63:0] rd_data;
    wire core_in_valid = fb_ov && fwd_frame;
    eeg_ssvep_etrca_core #(.NTGT(NTGT), .NH(NH), .NMODEL(NMODEL)) u_core(.clk(clk), .rst_n(rst_n), .start_now(start_evt),
        .prm_we(prm_we_r), .prm_addr(prm_addr_r), .prm_data(prm_data_r), .in_valid(core_in_valid), .in_idx(fb_oidx), .in_x(fb_ox),
        .score_req(sc_req), .model_sel(sc_model), .ready(core_ready), .frame_done(core_frame_done), .score_done(core_score_done),
        .nsamp(core_nsamp), .error_sticky(core_err), .rd_kind(rd_kind), .rd_idx(rd_idx), .rd_data(rd_data));

    always @(posedge clk) begin
        if (start_evt) win_clip <= 1'b0;
        else if (fb_ov && fwd_frame && fb_olast && fb_oclip) win_clip <= 1'b1;
    end

    // ================================================================= checkpoint trigger
    reg trig_pend, manual_pend, rec_manual;
    reg [2:0] trig_model;
    reg hit;
    reg [2:0] hit_m;
    integer mi;
    reg [31:0] pk;
    always @(*) begin
        hit = 1'b0; hit_m = 3'd0;
        for (mi = NMODEL - 1; mi >= 0; mi = mi - 1) begin
            pk = pmem[mi * 32];
            if (pk != 32'd0 && pk[15:0] == core_nsamp) begin hit = 1'b1; hit_m = mi[2:0]; end
        end
    end
    wire fin_idle = !fin_busy;
    wire fin_done;
    reg dec_busy;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin sc_req <= 0; sc_model <= 0; trig_pend <= 0; trig_model <= 0; manual_pend <= 0; rec_manual <= 0; end
        else begin
            sc_req <= 1'b0;
            if (core_frame_done && running && dec_en && hit && !decided) begin trig_pend <= 1'b1; trig_model <= hit_m; end
            if (ctrl_score) manual_pend <= 1'b1;
            if (trig_pend && core_ready && fin_idle && !dec_busy && !fin_done && !sc_req) begin
                sc_req <= 1'b1; sc_model <= trig_model; trig_pend <= 1'b0; rec_manual <= 1'b0;
            end else if (manual_pend && core_ready && fin_idle && !dec_busy && !fin_done && !fb_busy && !sc_req) begin
                sc_req <= 1'b1; sc_model <= model_manual; manual_pend <= 1'b0; rec_manual <= 1'b1;
            end
            if (ctrl_abort) begin trig_pend <= 1'b0; manual_pend <= 1'b0; end
        end
    end

    // ================================================================= finishing
    wire [7:0] pm_a;
    wire [31:0] pm_q = pmem[pm_a];
    wire signed [63:0] s_best, s_second;
    wire [7:0] best_i, second_i;
    wire [63:0] tr0, tr4;
    wire q_sat;
    reg [4:0] sres_a;
    reg [7:0] qres_a;
    wire signed [63:0] sres_q;
    wire [31:0] qres_q;
    eeg_ssvep_finish #(.NTGT(NTGT)) u_fin(.clk(clk), .rst_n(rst_n), .start(core_score_done), .model(sc_model), .rd_kind(f_rd_kind),
        .rd_idx(f_rd_idx), .rd_data(rd_data), .pm_a(pm_a), .pm_q(pm_q), .busy(fin_busy), .done(fin_done), .s_best(s_best),
        .s_second(s_second), .best_i(best_i), .second_i(second_i), .tr0(tr0), .tr4(tr4), .q_sat(q_sat), .sres_a(sres_a), .sres_q(sres_q),
        .qres_a(qres_a), .qres_q(qres_q));

    // ================================================================= decision / record builder
    localparam [3:0] D_IDLE = 0, D_G0 = 1, D_G1 = 2, D_G2 = 3, D_BUILD = 4, D_CRC = 5, D_W15 = 6, D_PUSH = 7;
    reg [3:0] ds;
    reg [2:0] rec_model;
    reg signed [63:0] thr_s, diff;
    reg [15:0] thr_m, emg_thr;
    reg [31:0] q_flat, q_huge;
    reg signed [81:0] mp, d16;
    reg [71:0] emg_l;
    reg [79:0] emg_r;
    reg [3:0] agree_cnt, agree_new;
    reg [7:0] agree_best;
    reg [11:0] flags;
    reg [31:0] rw [0:15];
    reg [4:0] crc_i;
    reg [31:0] crc_c;
    wire [31:0] crc_n;
    eeg_crc32w u_crc(.c(crc_c), .w(rw[crc_i[3:0]]), .n(crc_n));
    reg [31:0] rec_seq;
    reg rec_we;
    reg [7:0] rec_wa;
    reg [31:0] rec_wd;
    reg push_p;
    reg decided_now, final_now, gate_r, full_r;
    wire [3:0] agree_inc = (agree_cnt == 4'd15) ? 4'd15 : agree_cnt + 4'd1;
    wire [3:0] agree_if_gate = ((agree_cnt != 4'd0) && (agree_best == best_i)) ? agree_inc : 4'd1;
    reg dec_decided_r, dec_nodec_r, win_done_r;
    assign win_done_p = win_done_r;
    assign dec_decided_p = dec_decided_r;
    assign dec_nodec_p = dec_nodec_r;
    reg [7:0] base_m;
    wire [7:0] pm_base = {rec_model, 5'd0};
    wire score_ok = (s_best >= thr_s);
    wire margin_ok = (s_best > 64'sd0) && (d16 >= mp);
    wire f_flat = (tr0[47:16] < q_flat);
    wire f_huge = (tr0[47:16] > q_huge);
    wire f_emg = ({8'd0, emg_l} > emg_r);
    wire f_clip = win_clip;
    wire [3:0] qbits = {f_clip, f_emg, f_huge, f_flat};
    wire veto = |(qmask[3:0] & qbits);
    wire gate = score_ok && margin_ok && !veto;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ds <= D_IDLE; dec_busy <= 0; rec_we <= 0; push_p <= 0; decision_pulse <= 0; dec_decided_r <= 0; dec_nodec_r <= 0; win_done_r <= 0;
            agree_cnt <= 0; agree_best <= 0; rec_seq <= 0; rec_model <= 0; crc_i <= 0; crc_c <= 32'hffffffff;
        end else begin
            rec_we <= 1'b0; push_p <= 1'b0; decision_pulse <= 1'b0; dec_decided_r <= 1'b0; dec_nodec_r <= 1'b0; win_done_r <= 1'b0;
            if (start_evt) begin agree_cnt <= 0; agree_best <= 0; end
            case (ds)
            D_IDLE: begin
                dec_busy <= 1'b0;
                if (fin_done) begin ds <= D_G0; dec_busy <= 1'b1; rec_model <= sc_model; end
            end
            D_G0: begin
                thr_s <= {{8{pmem[pm_base + 8'd2][23]}}, pmem[pm_base + 8'd2][23:0], pmem[pm_base + 8'd1]};
                thr_m <= pmem[pm_base + 8'd3][15:0];
                q_flat <= pmem[pm_base + 8'd4];
                q_huge <= pmem[pm_base + 8'd5];
                emg_thr <= pmem[pm_base + 8'd6][15:0];
                diff <= s_best - s_second;
                ds <= D_G1;
            end
            D_G1: begin
                mp <= $signed({1'b0, thr_m}) * s_best;
                d16 <= diff <<< 16;
                emg_l <= {tr4, 8'd0};
                emg_r <= emg_thr * tr0;
                ds <= D_G2;
            end
            D_G2: begin
                gate_r <= gate;
                if (rec_manual) begin
                    agree_new <= agree_cnt; decided_now <= 1'b0; final_now <= 1'b0;
                end else begin
                    agree_new <= gate ? agree_if_gate : 4'd0;
                    decided_now <= gate && (agree_if_gate >= agree_n);
                    final_now <= (rec_model == final_model);
                end
                flags <= {rec_manual, !margin_ok, !score_ok, q_sat, qbits, 3'b000, gate};
                full_r <= (rec_cnt == 5'd16);
                ds <= D_BUILD;
            end
            D_BUILD: begin
                rw[0] <= 32'h31445353;
                rw[1] <= rec_seq;
                rw[2] <= cyc[31:0];
                rw[3] <= cyc[63:32];
                rw[4] <= win_start_seq;
                rw[5] <= {flags[11:4], final_now && !decided_now, final_now, decided_now, flags[0], 1'b0, rec_model, second_i, best_i};
                rw[6] <= {12'd0, agree_new, core_nsamp};
                rw[7] <= s_best[31:0];
                rw[8] <= s_best[63:32];
                rw[9] <= s_second[31:0];
                rw[10] <= s_second[63:32];
                rw[11] <= tr0[47:16];
                rw[12] <= cyc[31:0] - t_arr;
                rw[13] <= tr4[47:16];
                rw[14] <= cal_id;
                crc_i <= 5'd0; crc_c <= 32'hffffffff;
                ds <= D_CRC;
            end
            D_CRC: begin
                rec_we <= !full_r; rec_wa <= {rec_wp[3:0], crc_i[3:0]}; rec_wd <= rw[crc_i[3:0]];
                crc_c <= crc_n;
                if (crc_i == 5'd14) begin ds <= D_W15; crc_i <= 5'd15; end else crc_i <= crc_i + 5'd1;
            end
            D_W15: begin
                rec_we <= !full_r; rec_wa <= {rec_wp[3:0], 4'd15}; rec_wd <= ~crc_c;
                ds <= D_PUSH;
            end
            D_PUSH: begin
                push_p <= 1'b1;
                rec_seq <= rec_seq + 32'd1;
                if (!rec_manual) begin
                    agree_cnt <= agree_new;
                    if (gate_r) agree_best <= best_i;
                    if (decided_now) begin dec_decided_r <= 1'b1; decision_pulse <= 1'b1; end
                    if (final_now && !decided_now) dec_nodec_r <= 1'b1;
                    if (decided_now || final_now) win_done_r <= 1'b1;
                end
                ds <= D_IDLE;
            end
            default: ds <= D_IDLE;
            endcase
        end
    end

    // ================================================================= record FIFO
    (* ram_style = "distributed" *) reg [31:0] rec_ram [0:255];
    always @(posedge clk) if (rec_we) rec_ram[rec_wa] <= rec_wd;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin rec_wp <= 0; rec_rp <= 0; rec_cnt <= 0; rec_overflow <= 0; end
        else begin
            if (ctrl_clr_fifo) begin rec_wp <= 0; rec_rp <= 0; rec_cnt <= 0; end
            else begin
                if (push_p) begin
                    if (rec_cnt == 5'd16 && !pop_p) rec_overflow <= 1'b1;
                    else begin rec_wp <= rec_wp + 4'd1; if (!pop_p) rec_cnt <= rec_cnt + 5'd1; end
                end
                if (pop_p) begin rec_rp <= rec_rp + 4'd1; if (!push_p) rec_cnt <= rec_cnt - 5'd1; end
            end
            if (ctrl_clr_err) rec_overflow <= 1'b0;
        end
    end

    // ================================================================= latency statistics and histograms
    (* ram_style = "distributed" *) reg [31:0] hist_ck [0:63];
    (* ram_style = "distributed" *) reg [31:0] hist_fr [0:63];
    integer hi_;
    initial begin
        for (hi_ = 0; hi_ < 64; hi_ = hi_ + 1) begin hist_ck[hi_] = 32'd0; hist_fr[hi_] = 32'd0; end
    end
    reg [31:0] ck_min, ck_max, ck_cnt, fr_min, fr_max, fr_cnt;
    reg [63:0] ck_sum, fr_sum;
    reg [6:0] hclr_i;
    reg hclr;
    wire [31:0] lat_fr = cyc[31:0] - t_arr;
    wire [31:0] lat_ck = cyc[31:0] - t_arr;
    wire [5:0] bin_fr = (lat_fr[31:12] > 20'd63) ? 6'd63 : lat_fr[17:12];
    wire [5:0] bin_ck = (lat_ck[31:12] > 20'd63) ? 6'd63 : lat_ck[17:12];
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ck_min <= 32'hffffffff; ck_max <= 0; ck_cnt <= 0; ck_sum <= 0; fr_min <= 32'hffffffff; fr_max <= 0; fr_cnt <= 0; fr_sum <= 0;
            hclr <= 0; hclr_i <= 0;
        end else begin
            if (ctrl_clr_hist) begin
                hclr <= 1'b1; hclr_i <= 7'd0;
                ck_min <= 32'hffffffff; ck_max <= 0; ck_cnt <= 0; ck_sum <= 0; fr_min <= 32'hffffffff; fr_max <= 0; fr_cnt <= 0; fr_sum <= 0;
            end else if (hclr) begin
                hist_ck[hclr_i[5:0]] <= 32'd0; hist_fr[hclr_i[5:0]] <= 32'd0;
                if (hclr_i == 7'd63) hclr <= 1'b0; else hclr_i <= hclr_i + 7'd1;
            end else begin
                if (core_frame_done && running) begin
                    hist_fr[bin_fr] <= hist_fr[bin_fr] + 32'd1;
                    fr_cnt <= fr_cnt + 32'd1; fr_sum <= fr_sum + {32'd0, lat_fr};
                    if (lat_fr < fr_min) fr_min <= lat_fr;
                    if (lat_fr > fr_max) fr_max <= lat_fr;
                end
                if (push_p && !rec_manual) begin
                    hist_ck[bin_ck] <= hist_ck[bin_ck] + 32'd1;
                    ck_cnt <= ck_cnt + 32'd1; ck_sum <= ck_sum + {32'd0, lat_ck};
                    if (lat_ck < ck_min) ck_min <= lat_ck;
                    if (lat_ck > ck_max) ck_max <= lat_ck;
                end
            end
        end
    end

    // ================================================================= capture RAM (sub-band samples of the window, two per word)
    (* ram_style = "block" *) reg [31:0] cap_mem [0:CAP_WORDS-1];
    reg [14:0] cap_wp, cap_ra;
    reg [15:0] cap_frames;
    reg cap_done;
    reg signed [15:0] cap_prev;
    reg [31:0] cap_q;
    reg cap_we;
    reg [31:0] cap_wd;
    always @(posedge clk) begin
        if (cap_we) cap_mem[cap_wp] <= cap_wd;
        cap_q <= cap_mem[cap_ra];
    end
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin cap_wp <= 0; cap_frames <= 0; cap_done <= 0; cap_we <= 0; cap_prev <= 0; cap_wd <= 0; end
        else begin
            cap_we <= 1'b0;
            if (ctrl_cap_reset || start_evt) begin cap_wp <= 0; cap_frames <= 0; cap_done <= 0; end
            else if (fb_ov && fwd_frame && cap_en && !cap_done) begin
                cap_prev <= fb_ox;
                if (fb_oidx[0]) begin cap_we <= 1'b1; cap_wd <= {fb_ox, cap_prev}; end
                if (cap_we) cap_wp <= cap_wp + 15'd1;
                if (fb_olast) begin
                    cap_frames <= cap_frames + 16'd1;
                    if (cap_frames + 16'd1 >= cap_max || cap_frames + 16'd1 >= CAP_FRAMES) cap_done <= 1'b1;
                end
            end else if (cap_we) cap_wp <= cap_wp + 15'd1;
        end
    end

    // ================================================================= window write block
    wire [9:0] w_idx = x_waddr[11:2] - 10'h80;
    wire w_inwin = (x_waddr[11:7] == 5'd4);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cfg <= 32'h0; arm_seq <= 0; qmask <= 32'h0000000f; cal_id <= 0; paddr <= 0; cap_max <= 16'd1024; ctrl_r <= 9'd0; feed_wr <= 0;
            feed_word <= 0; prm_we_r <= 0; prm_addr_r <= 0; prm_data_r <= 0; x_wack <= 0; x_werr <= 0; pc_st <= 0; pc_a <= 0; pc_d <= 0;
            pcrc <= 32'hffffffff; err_bits <= 0; cap_rp <= 0;
        end else begin
            ctrl_r <= 9'd0; feed_wr <= 1'b0; prm_we_r <= 1'b0; x_wack <= 1'b0; x_werr <= 1'b0;
            // parameter CRC (two words per write)
            case (pc_st)
                3'd1: begin pcrc <= pcrc_n; pc_st <= 3'd2; end
                3'd2: begin pcrc <= pcrc_n; pc_st <= 3'd0; end
                default: begin end
            endcase
            if (x_wen) begin
                x_wack <= 1'b1;
                if (!w_inwin) begin x_werr <= 1'b1; end
                else case (w_idx[6:0])
                    7'd1: ctrl_r <= x_wdata[8:0];
                    7'd4: arm_seq <= x_wdata;
                    7'd6: cfg <= x_wdata;
                    7'd7: paddr <= x_wdata;
                    7'd8: begin
                        prm_we_r <= 1'b1; prm_addr_r <= paddr[17:0]; prm_data_r <= x_wdata;
                        if (paddr[17:8] == 10'h001) pmem[paddr[7:0]] <= x_wdata;
                        pc_a <= {14'd0, paddr[17:0]}; pc_d <= x_wdata; pc_st <= 3'd1;
                        paddr <= paddr + 32'd1;
                    end
                    7'd9: begin feed_wr <= 1'b1; feed_word <= x_wdata; end
                    7'd14: qmask <= x_wdata;
                    7'd15: cap_rp <= x_wdata[14:0];
                    7'd18: cap_max <= x_wdata[15:0];
                    7'd31: cal_id <= x_wdata;
                    default: x_werr <= 1'b1;
                endcase
            end
            if (ctrl_clr_pcrc) pcrc <= 32'hffffffff;
            if (ctrl_clr_err) err_bits <= 32'd0;
            if (core_err) err_bits[0] <= 1'b1;
            if (fb_drop) err_bits[1] <= 1'b1;
            if (rec_overflow) err_bits[2] <= 1'b1;
            if (err_set_rdconf) err_bits[3] <= 1'b1;
            if (arm_missed) err_bits[4] <= 1'b1;
            if (rd_cap_inc) cap_rp <= cap_rp + 15'd1;
            if (rd_paddr_inc) paddr <= paddr + 32'd1;
        end
    end
    eeg_crc32w u_pcrc(.c(pcrc), .w(pc_st == 3'd1 ? pc_a : pc_d), .n(pcrc_n));

    // ================================================================= window read pipeline (fixed 4-clock latency)
    reg [2:0] rp_st;
    reg [9:0] rd_w;
    reg rd_inwin;
    reg [31:0] rd_val;
    reg rd_err;
    wire [31:0] status_w = {trig_pend, fin_busy, feed_mode, no_decision, last_best_r, 1'b0, last_model_r, 2'b00, rec_cnt,
                            (rec_cnt != 5'd0), cap_done, arm_missed, rec_overflow, (|err_bits), decided, running, armed,
                            (fb_busy | !core_ready | fin_busy | dec_busy)};
    always @(posedge clk) begin
        if (push_p) begin last_best_r <= best_i; last_model_r <= rec_model; end
    end

    // pointer-space read decode (PDATA)
    wire [3:0] sp = paddr[15:12];
    wire pd_low = (paddr[17:16] == 2'b00);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rp_st <= 0; x_rvalid <= 0; x_rdata <= 0; x_rerr <= 0; rd_w <= 0; rd_inwin <= 0; rd_val <= 0; rd_err <= 0; rec_rw <= 0; pop_p <= 0;
            rd_cap_inc <= 0; rd_paddr_inc <= 0; err_set_rdconf <= 0; ps_rd_kind <= 0; ps_rd_idx <= 0; sres_a <= 0; qres_a <= 0; cap_ra <= 0;
        end else begin
            x_rvalid <= 1'b0; x_rerr <= 1'b0; pop_p <= 1'b0; rd_cap_inc <= 1'b0; rd_paddr_inc <= 1'b0; err_set_rdconf <= 1'b0;
            case (rp_st)
            3'd0: if (x_ren) begin rd_w <= x_raddr[11:2] - 10'h80; rd_inwin <= (x_raddr[11:7] == 5'd4); rp_st <= 3'd1; end
            3'd1: begin
                // set up memory addresses
                if (!rd_inwin) begin rd_err <= 1'b1; end else rd_err <= 1'b0;
                ps_rd_kind <= (sp == 4'd4) ? 2'd1 : (sp == 4'd5) ? 2'd2 : 2'd0;
                ps_rd_idx <= (sp == 4'd3) ? {1'b0, paddr[8:1]} : {6'd0, paddr[3:1]};
                sres_a <= paddr[5:1];
                qres_a <= paddr[7:0];
                cap_ra <= cap_rp;
                rp_st <= 3'd2;
            end
            3'd2: rp_st <= 3'd3;
            3'd3: begin
                rd_val <= 32'd0;
                case (rd_w[6:0])
                    7'd0: rd_val <= 32'h45545232;
                    7'd2: rd_val <= status_w;
                    7'd3: rd_val <= {16'd0, core_nsamp};
                    7'd4: rd_val <= arm_seq;
                    7'd5: rd_val <= cur_seq;
                    7'd6: rd_val <= cfg;
                    7'd7: rd_val <= paddr;
                    7'd8: begin
                        rd_paddr_inc <= 1'b1;
                        if (pd_low && paddr[15:8] == 8'h01) rd_val <= pmem[paddr[7:0]];
                        else if (pd_low && (sp == 4'd3 || sp == 4'd4 || sp == 4'd5)) begin
                            if (fin_busy) begin rd_val <= 32'hdead0001; err_set_rdconf <= 1'b1; end
                            else rd_val <= paddr[0] ? rd_data[63:32] : rd_data[31:0];
                        end
                        else if (pd_low && sp == 4'd6) rd_val <= paddr[0] ? sres_q[63:32] : sres_q[31:0];
                        else if (pd_low && sp == 4'd7) rd_val <= qres_q;
                        else if (pd_low && sp == 4'd8) rd_val <= hist_ck[paddr[5:0]];
                        else if (pd_low && sp == 4'd9) rd_val <= hist_fr[paddr[5:0]];
                        else if (pd_low && sp == 4'd10) begin
                            case (paddr[3:0])
                                4'd0: rd_val <= {24'd0, last_best_r};
                                4'd1: rd_val <= {29'd0, last_model_r};
                                4'd2: rd_val <= core_nsamp;
                                4'd3: rd_val <= win_start_seq;
                                4'd4: rd_val <= rec_seq;
                                4'd5: rd_val <= {8'd1, 8'd0, NM8, NT8};
                                default: rd_val <= 32'd0;
                            endcase
                        end
                    end
                    7'd10: begin
                        if (rec_cnt != 5'd0) begin
                            rd_val <= rec_ram[{rec_rp, rec_rw}];
                            rec_rw <= rec_rw + 4'd1;
                            if (rec_rw == 4'd15) pop_p <= 1'b1;
                        end
                    end
                    7'd11: rd_val <= {16'd0, rec_seq[7:0], 3'd0, rec_cnt};
                    7'd12: begin rd_val <= cyc[31:0]; cyc_hi_l <= cyc[63:32]; end
                    7'd13: rd_val <= cyc_hi_l;
                    7'd14: rd_val <= qmask;
                    7'd15: rd_val <= {17'd0, cap_rp};
                    7'd16: begin rd_val <= cap_q; rd_cap_inc <= 1'b1; end
                    7'd17: rd_val <= {16'd0, cap_frames};
                    7'd18: rd_val <= {16'd0, cap_max};
                    7'd19: rd_val <= ck_min;
                    7'd20: rd_val <= ck_max;
                    7'd21: rd_val <= ck_sum[31:0];
                    7'd22: rd_val <= ck_sum[63:32];
                    7'd23: rd_val <= ck_cnt;
                    7'd24: rd_val <= fr_min;
                    7'd25: rd_val <= fr_max;
                    7'd26: rd_val <= fr_sum[31:0];
                    7'd27: rd_val <= fr_sum[63:32];
                    7'd28: rd_val <= fr_cnt;
                    7'd29: rd_val <= pcrc;
                    7'd30: rd_val <= err_bits;
                    7'd31: rd_val <= cal_id;
                    7'd32: rd_val <= {8'd1, 8'd0, NM8, NT8};
                    default: begin end
                endcase
                rp_st <= 3'd4;
            end
            3'd4: begin
                x_rdata <= rd_val; x_rvalid <= 1'b1; x_rerr <= rd_err; rp_st <= 3'd0;
            end
            default: rp_st <= 3'd0;
            endcase
            if (ctrl_clr_fifo) rec_rw <= 4'd0;
        end
    end
endmodule
