`timescale 1ns/1ps
// CANDIDATE (simulation verified; OOC numbers in docs/fs_migration_ab_20261101.md; not integrated in a released bitstream).
// EDP3 bridge for the 976.5625 SPS front end.  Input: the unchanged EMD1 frame stream (24 words/frame, TLAST every 32
// frames from axis_cont_packetizer_terminal_v1, word 22 = 1,024,000 ns).  Output: one EDP3 packet per 128 raw frames
// (131.072 ms, i.e. the same DMA cadence and FFT cadence as EDP2):
//
//   words      content
//   0..31      header (128 B)
//   32..3103   128 raw EMD1 frames, 96 B each, unchanged format (976.5625 SPS)
//   3104..3679 32 decimated filtered frames x 18 words {dseq, clip_mask, 16 x S24 in S32}: raw->4:1 FIR->original Q30 filter16
//              (244.140625 SPS, FIR group delay ~30 ms + the old filter's)
//   3680..4191 128 ECG records x 4 words {raw_seq, diff25 (sign-extended), y (Q4 fixed point S32), flags}
//   4192..4239 up to 8 candidate-QRS events x 6 words {raw_seq_of_peak, idx, pk[31:0], thr[31:0], {thr[35:32],pk[35:32]}, 0}
//   4240..4255 ECG/decimator status block (16 words)
//   4256..5279 256 spectrum records x 4 words (unchanged record format, FFT over 2048 DECIMATED samples)
//   5280       CRC32 over words 0..5279 (same polynomial/convention as EDP2)
//   5281..5311 zero padding              (packet = 5312 words = 21,248 B)
//   FFT16=1 (flag 0x800, header word 15 = 0xffff): every spectrum window is channels 8..15 x 1025 bins (the EDP2 contract) followed by
//   channels 0..7 x bins 0..511 (0..61 Hz): 12,296 records, 49 packets of the 64 available per 2048-sample window.
//   HEALTH=1 (flag 0x400): 5280..5309 per-channel signal health (eeg_signal_health_v1: mean, rms code, 50 Hz code, rail hits, flags),
//   5310 CRC32 over words 0..5309, 5311 zero.  The packet size and every other section are unchanged.
//
// Memories are single-write/sync-read so they map to block RAM; the packet is produced by a 2-stage prefetch pipeline
// into a 4-entry output FIFO (no combinational path from m_axis_tready to a memory).  CRC is accumulated as words enter
// the FIFO.  The 2048-point FFT, Q30 filter16 and spectrum snapshot logic are the EDP2 ones running on the decimated
// stream; spectrum window sequence numbers are in decimated units (raw_seq >> 2).
// mode_ecg must only change while stopped (flush).  In EEG16 mode the ECG sections are zero and flag 0x100 is clear.
module eeg_dsp_edp3_axis_bridge_v3 #(
    parameter TWIDDLE_INIT_FILE="twiddle_q15_v2.hex",
    parameter HANN_INIT_FILE="hann_periodic_q15_v2.hex",
    parameter DEC_COEF_FILE="dec_coef.hex",
    parameter [31:0] SAMPLE_INTERVAL_NS = 32'd1024000,
    parameter integer HEALTH = 0,
    parameter integer FFT16 = 0        // 1: after channels 8..15 (1025 bins, as before) the FFT also runs channels 0..7, bins 0..511 only
)(
    input wire clk,
    input wire rst_n,
    input wire flush,
    input wire mode_ecg,
    input wire s_axis_tvalid,
    output wire s_axis_tready,
    input wire [31:0] s_axis_tdata,
    input wire [3:0] s_axis_tkeep,
    input wire s_axis_tlast,
    input wire [63:0] s_axis_frame_cycle,
    output wire [31:0] m_axis_tdata,
    output wire [3:0] m_axis_tkeep,
    output wire m_axis_tvalid,
    input wire m_axis_tready,
    output wire m_axis_tlast,
    output reg input_error,
    output reg fft_window_overrun,
    output reg filter_clipped_sticky,
    output reg [31:0] reset_epoch,
    output reg [31:0] packets_emitted,
    // decimator output tap (244.140625 SPS, channels 0..15 in order, tap_seq = decimated sequence of the group): feeds the SSVEP unit
    output wire tap_valid,
    output wire [3:0] tap_ch,
    output wire signed [23:0] tap_x,
    output wire tap_sat,
    output wire [31:0] tap_seq
);
    localparam [2:0] S_CAPTURE=3'd0, S_ECG=3'd1, S_ECGW=3'd2, S_DEC=3'd3, S_FILT_WAIT=3'd4,
                     S_SNAPSHOT=3'd5, S_OUTPUT=3'd6, S_FAULT=3'd7;
    localparam [2:0] F_IDLE=3'd0, F_RING_READ=3'd1, F_LOAD=3'd2, F_WAIT=3'd3, F_DRAIN=3'd4;
    localparam integer RAW_FRAMES=128, PACKET_WORDS=5312, CRC_WORD=(HEALTH!=0)?5310:5280;
    reg [2:0] state=S_CAPTURE;
    reg [2:0] fft_state=F_IDLE;

    (* ram_style="block" *) reg [31:0] raw_mem[0:3071];
    (* ram_style="block" *) reg [31:0] filt_mem[0:575];        // output order: 32 x {dseq, clip, 16 samples}
    (* ram_style="block" *) reg [31:0] ecg_mem[0:511];
    reg [31:0] ev_mem[0:47];
    (* ram_style="block" *) reg [127:0] spectrum_mem[0:255];
    wire signed [23:0] fft_ring_bank_q[0:15];                    // bank b holds channel (b+8) mod 16; banks 8..15 only exist with FFT16
    reg [15:0] bin_index_mem[0:255];
    reg [3:0] bin_channel_mem[0:255];
    reg signed [31:0] bin_real_mem[0:255];
    reg signed [31:0] bin_imag_mem[0:255];
    reg [31:0] bin_start_mem[0:255];
    reg [31:0] bin_end_mem[0:255];

    reg [6:0] frame_index=0;
    reg [4:0] word_index=0;
    reg [3:0] ra_ch=0;                                          // feeder read-address channel
    reg [1:0] ecg_ph=0;
    reg [4:0] filter_output_count=0;
    reg [31:0] frame_sequence=0;
    reg [31:0] expected_sequence=0;
    reg sequence_initialized=0;
    reg [31:0] packet_first_sequence=0;
    reg [63:0] packet_first_cycle=0,packet_last_cycle=0;
    reg [31:0] packet_sequence=0;
    reg [31:0] packet_clipped_frames=0;
    reg [31:0] packet_flags=0;
    reg [31:0] frame_clip_mask=0;
    reg [15:0] dec_clip_acc=0,dec_clip_lat=0;
    reg ecg_clip_lat=0;
    reg [3:0] ev_count=0;
    reg ecg_mode_lat=0;
    reg [31:0] window_end_sequence=0;
    reg [31:0] window_generation=0;

    reg [8:0] bin_count=0;
    reg [7:0] bin_write=0;
    reg [7:0] bin_read=0;
    reg [8:0] snapshot_target=0;
    reg [8:0] snapshot_index=0;
    reg [8:0] packet_spectrum_count=0;
    reg [31:0] packet_fft_start=32'hffffffff;
    reg [31:0] packet_fft_end=32'hffffffff;
    reg [31:0] crc_state=32'hffffffff;

    reg [31:0] fft_seen_generation=0;
    reg [31:0] fft_active_generation=0;
    reg [31:0] fft_active_end=0;
    reg [10:0] fft_load_index=0;
    reg [3:0] fft_channel=8;
    wire signed [23:0] fft_ring_sample;
    wire fft_core_ready,fft_core_busy,fft_core_overrun,fft_bin_valid,fft_bin_ready;
    wire [10:0] fft_bin_index;
    wire [3:0] fft_bin_channel;
    wire signed [31:0] fft_bin_real,fft_bin_imag;

    wire [31:0] dseq = {2'b00, frame_sequence[31:2]};          // decimated sequence of the group that just completed
    wire [11:0] ring_read_addr={fft_active_end[11],fft_load_index};
    assign fft_ring_sample=fft_ring_bank_q[fft_channel-4'd8];
    eeg_fft2048_serial_v2 #(.TWIDDLE_INIT_FILE(TWIDDLE_INIT_FILE),.HANN_INIT_FILE(HANN_INIT_FILE)) fft_core(
        .clk(clk),.rst_n(rst_n),.flush(flush),
        .in_valid(fft_state==F_LOAD && fft_core_ready),.in_channel(fft_channel),.in_sample(fft_ring_sample),
        .in_ready(fft_core_ready),.busy(fft_core_busy),.input_overrun(fft_core_overrun),
        .bin_valid(fft_bin_valid),.bin_ready(fft_bin_ready),.bin_index(fft_bin_index),
        .bin_channel(fft_bin_channel),.bin_real(fft_bin_real),.bin_imag(fft_bin_imag));

    // ------------------------------------------------------------------ raw RAM: one write port, one read port
    // The three users (capture writes, feeders, packet output) are mutually exclusive in time.
    wire [11:0] frame_base = {5'd0,frame_index}*12'd24;
    reg [11:0] raw_ra;
    reg [31:0] raw_q;
    reg [12:0] pw=0;                                            // output word being issued
    always_comb begin
        raw_ra = 12'd0;
        case(state)
            S_ECG:    raw_ra = frame_base + 12'd6 + ((ecg_ph==2'd0) ? 12'd0 : 12'd1);
            S_DEC:    raw_ra = frame_base + 12'd6 + {8'd0,ra_ch};           // words 6..21 = channels 0..15
            S_OUTPUT: raw_ra = pw[11:0] - 12'd32;
            default:  raw_ra = 12'd0;
        endcase
    end
    always @(posedge clk) raw_q <= raw_mem[raw_ra];

    function rail(input [23:0] v); rail = (v==24'h7fffff)||(v==24'h800000); endfunction

    wire parser_bad=(s_axis_tkeep!=4'hf) ||
        (s_axis_tlast!=((frame_index[4:0]==5'd31)&&(word_index==23))) ||
        (word_index==0 && s_axis_tdata!=32'h31444d45) ||
        (word_index==1 && s_axis_tdata!=32'h00600001) ||
        (word_index==2 && sequence_initialized && s_axis_tdata!=expected_sequence) ||
        (word_index==2 && frame_index==0 && s_axis_tdata[6:0]!=7'd0) ||      // packets start on a 128-frame boundary
        (word_index==3 && s_axis_tdata!=frame_sequence) ||
        ((word_index==4 || word_index==5) && s_axis_tdata[23:20]!=4'hc) ||
        (word_index==22 && s_axis_tdata!=SAMPLE_INTERVAL_NS) ||
        (word_index==23 && s_axis_tdata!=32'h00000103);

    // ------------------------------------------------------------------ 4:1 decimator -> original Q30 filter16
    reg d_started=0, dec_issue=0, dv=0;
    reg [3:0] dch=0;
    wire dec_in_valid = dv;
    wire dec_in_ready;
    wire dec_out_valid,dec_out_sat,dec_out_warm,dec_out_last;
    wire [3:0] dec_out_channel;
    wire signed [23:0] dec_out_sample;
    wire [31:0] dec_frames_in;
    eeg_decim4_fir_v3 #(.TAPS(60),.COEF_FILE(DEC_COEF_FILE)) decim(
        .clk(clk),.rst_n(rst_n),.flush(flush || state==S_FAULT),
        .in_valid(dec_in_valid),.in_channel(dch),.in_sample(raw_q[23:0]),.in_ready(dec_in_ready),
        .out_valid(dec_out_valid),.out_channel(dec_out_channel),.out_sample(dec_out_sample),
        .out_sat(dec_out_sat),.out_warm(dec_out_warm),.out_last(dec_out_last),.frames_in(dec_frames_in));

    // ------------------------------------------------------------------ per-channel signal health (HEALTH=1)
    wire health_done;
    wire [31:0] hlt_q;                                           // health word, registered inside eeg_signal_health_v1
    wire health_clear = (state==S_CAPTURE) && s_axis_tvalid && s_axis_tready && !parser_bad && frame_index==0 && word_index==0;
    wire health_fin = (state==S_DEC) && dv && dch==4'd15 && frame_index==RAW_FRAMES-1;
    generate if(HEALTH!=0)begin : g_health
        eeg_signal_health_v1 health(.clk(clk),.rst_n(rst_n),.flush(flush || state==S_FAULT),.clear(health_clear),
            .in_valid((state==S_DEC) && dv),.in_ch(dch),.in_x(raw_q[23:0]),.in_n(frame_index),.fin_start(health_fin),
            .done(health_done),.rd_idx((pw-13'd5280)),.rd_word(hlt_q));
    end else begin : g_nohealth
        assign health_done=1'b1;
        assign hlt_q=32'd0;
    end endgenerate

    wire filter_out_valid,filter_out_clipped;
    wire [3:0] filter_out_channel;
    wire signed [23:0] filter_out_sample;
    eeg_dsp_filter16_q30_v2 filter16(
        .clk(clk),.rst_n(rst_n),.flush(flush || state==S_FAULT),
        .in_valid(dec_out_valid),.in_clipped(dec_out_sat | dec_clip_lat[dec_out_channel]),.in_channel(dec_out_channel),
        .in_sample(dec_out_sample),.out_valid(filter_out_valid),
        .out_clipped(filter_out_clipped),.out_channel(filter_out_channel),.out_sample(filter_out_sample));

    // ------------------------------------------------------------------ full-rate ECG front end
    reg [31:0] ecg_a=0, ecg_b=0;
    wire ecg_in_valid=(state==S_ECG) && (ecg_ph==2'd3);
    wire ecg_in_ready,ecg_out_valid,ecg_ev_valid;
    wire signed [24:0] ecg_out_diff;
    wire signed [31:0] ecg_out_y;
    wire signed [17:0] ecg_out_dvs;
    wire [35:0] ecg_out_mwi,ecg_ev_pk,ecg_ev_thr;
    wire [31:0] ecg_ev_idx,ecg_c_samples,ecg_c_clipped,ecg_c_sat,ecg_c_events,ecg_c_flat,ecg_c_noise;
    wire ecg_rail = rail(ecg_a[23:0]) || rail(ecg_b[23:0]);
    eeg_ecg_front_v3 ecg(
        .clk(clk),.rst_n(rst_n),.flush(flush || state==S_FAULT),.mode_ecg(mode_ecg),
        .in_valid(ecg_in_valid),.in_a(ecg_a[23:0]),.in_b(ecg_b[23:0]),.in_clipped(ecg_rail),.in_ready(ecg_in_ready),
        .out_valid(ecg_out_valid),.out_diff(ecg_out_diff),.out_y(ecg_out_y),.out_dvs(ecg_out_dvs),.out_mwi(ecg_out_mwi),
        .ev_valid(ecg_ev_valid),.ev_idx(ecg_ev_idx),.ev_pk(ecg_ev_pk),.ev_thr(ecg_ev_thr),
        .cnt_samples(ecg_c_samples),.cnt_clipped(ecg_c_clipped),.cnt_sat(ecg_c_sat),.cnt_events(ecg_c_events),
        .cnt_flat_runs(ecg_c_flat),.cnt_noise_peaks(ecg_c_noise));

    // ------------------------------------------------------------------ spectrum bin FIFO and FFT scheduling (EDP2 logic)
    // FFT16: channels 0..7 keep only bins 0..511; the dropped bins are consumed from the core without entering the record FIFO
    wire fft_drop = (FFT16!=0) && (fft_bin_channel<4'd8) && (fft_bin_index>=11'd512);
    wire bin_take=(fft_state==F_DRAIN)&&fft_bin_valid&&fft_bin_ready;
    wire bin_push=bin_take&&!fft_drop;
    wire fft_last_channel = (FFT16!=0) ? (fft_channel==4'd7) : (fft_channel==4'd15);
    wire bin_pop=(state==S_SNAPSHOT)&&(snapshot_index<snapshot_target)&&
        (bin_start_mem[bin_read]==packet_fft_start)&&(bin_end_mem[bin_read]==packet_fft_end);
    assign fft_bin_ready=(fft_state==F_DRAIN)&&(fft_drop || bin_count<9'd256);
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n)begin bin_count<=0;bin_write<=0;bin_read<=0;end
        else if(flush)begin bin_count<=0;bin_write<=0;bin_read<=0;end
        else begin
            if(bin_push)bin_write<=bin_write+1'b1;
            if(bin_pop)bin_read<=bin_read+1'b1;
            case({bin_push,bin_pop})
                2'b10:bin_count<=bin_count+1'b1;
                2'b01:bin_count<=bin_count-1'b1;
                default:bin_count<=bin_count;
            endcase
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if(!rst_n)begin
            fft_state<=F_IDLE;fft_seen_generation<=0;fft_active_generation<=0;fft_active_end<=0;
            fft_load_index<=0;fft_channel<=8;
        end else if(flush)begin
            fft_state<=F_IDLE;fft_seen_generation<=0;fft_active_generation<=0;
            fft_active_end<=0;fft_load_index<=0;fft_channel<=8;
        end else begin
            case(fft_state)
            F_IDLE:if(window_generation!=fft_seen_generation)begin
                    fft_active_generation<=window_generation;fft_seen_generation<=window_generation;
                fft_active_end<=window_end_sequence;fft_channel<=8;fft_load_index<=0;fft_state<=F_RING_READ;
                end
            F_RING_READ:fft_state<=F_LOAD;
            F_LOAD:if(fft_core_ready)begin
                    if(fft_load_index==11'd2047)begin fft_load_index<=0;fft_state<=F_WAIT;end
                else begin fft_load_index<=fft_load_index+1'b1;fft_state<=F_RING_READ;end
                end
                F_WAIT:if(fft_bin_valid)fft_state<=F_DRAIN;
                F_DRAIN:if(bin_take && fft_bin_index==11'd1024)begin
                    if(!fft_last_channel)begin fft_channel<=fft_channel+1'b1;fft_load_index<=0;fft_state<=F_RING_READ;end
                    else if(window_generation!=fft_seen_generation)begin
                        fft_active_generation<=window_generation;fft_seen_generation<=window_generation;
                        fft_active_end<=window_end_sequence;fft_channel<=8;fft_load_index<=0;fft_state<=F_RING_READ;
                    end else begin fft_state<=F_IDLE;fft_channel<=8;end
                end
                default:begin fft_state<=F_IDLE;fft_channel<=8;fft_load_index<=0;end
            endcase
        end
    end

    function [31:0] crc32_word(input [31:0] c,input [31:0] w);
        reg [31:0] v;
        integer b,k;
        begin
            v=c;
            for(b=0;b<4;b=b+1)begin
                v=v^{w[b*8+:8]};
                for(k=0;k<8;k=k+1)v=(v>>1)^(32'hedb88320 & (0-(v&1)));
            end
            crc32_word=v;
        end
    endfunction

    function [31:0] header_word(input [4:0] idx);
        begin
            case(idx)
                0:header_word=32'h33504445;                       // 'EDP3'
                1:header_word=32'h00800003;                       // version 3, header 128 B
                2:header_word=32'd21248;
                3:header_word=32'h00000003 | ((packet_spectrum_count!=0)?32'h4:0) |
                    (packet_clipped_frames?32'h20:0) | (packet_flags&32'h2d8) | (ecg_mode_lat?32'h100:0) | ((HEALTH!=0)?32'h400:32'h0) | ((FFT16!=0)?32'h800:32'h0);
                4:header_word=packet_sequence;
                5:header_word=packet_first_sequence;
                6:header_word=32'd128;                            // raw frames per packet
                7:header_word=SAMPLE_INTERVAL_NS;                 // raw sample interval
                8:header_word=32'd12288;                          // raw bytes
                9:header_word=32'd2304;                           // decimated filtered bytes (32 x 72)
                10:header_word=packet_spectrum_count;
                11:header_word=32'd256;
                12:header_word=32'd2048;
                13:header_word=packet_fft_start;                  // decimated-sequence units
                14:header_word=packet_fft_end;
                15:header_word=(FFT16!=0)?32'h0000ffff:32'h0000ff00;
                16:header_word=32'd16;
                17:header_word=32'h00030001;                      // algorithm id: multirate v3
                18:header_word=32'h00010002;                      // filter profile: Q30 244 Hz behind 4:1 FIR
                19:header_word=32'd1;
                20:header_word=32'd1;
                21:header_word=32'd2048;
                22:header_word=32'd1000000000;
                23:header_word=SAMPLE_INTERVAL_NS;
                24:header_word=32'd0;
                25:header_word=packet_clipped_frames;
                26:header_word=reset_epoch;
                27:header_word=32'd2;
                28:header_word=packet_first_cycle[31:0];
                29:header_word=packet_first_cycle[63:32];
                30:header_word=packet_last_cycle[31:0];
                31:header_word=packet_last_cycle[63:32];
                default:header_word=0;
            endcase
        end
    endfunction

    function [31:0] status_word(input [3:0] idx);
        begin
            case(idx)
                0:status_word={31'd0,ecg_mode_lat};
                1:status_word=32'd4;                              // decimation factor
                2:status_word=32'd4096000;                        // decimated sample interval ns
                3:status_word=32'h00020001;                       // ECG front profile id (HP0.5/notch50/LP45, error-feedback)
                4:status_word=ecg_c_samples;
                5:status_word=ecg_c_clipped;
                6:status_word=ecg_c_sat;
                7:status_word=ecg_c_events;
                8:status_word=ecg_c_flat;
                9:status_word=ecg_c_noise;
                10:status_word={28'd0,ev_count};
                11:status_word=32'd60;                            // FIR taps
                12:status_word=32'd59;                            // FIR group delay x2 in raw samples
                default:status_word=0;
            endcase
        end
    endfunction

    // ------------------------------------------------------------------ packet output: issue -> sync read -> FIFO
    localparam [3:0] SEC_HDR=4'd0, SEC_RAW=4'd1, SEC_FILT=4'd2, SEC_ECG=4'd3, SEC_EV=4'd4, SEC_ST=4'd5,
                     SEC_SPEC=4'd6, SEC_CRC=4'd7, SEC_PAD=4'd8, SEC_HLT=4'd9;
    reg [3:0] sec0;                                              // section of word pw (stage 0)
    always_comb begin
        if(pw<13'd32) sec0=SEC_HDR;
        else if(pw<13'd3104) sec0=SEC_RAW;
        else if(pw<13'd3680) sec0=SEC_FILT;
        else if(pw<13'd4192) sec0=SEC_ECG;
        else if(pw<13'd4240) sec0=SEC_EV;
        else if(pw<13'd4256) sec0=SEC_ST;
        else if(pw<13'd5280) sec0=SEC_SPEC;
        else if(HEALTH!=0 && pw<13'd5310) sec0=SEC_HLT;
        else if(pw==CRC_WORD) sec0=SEC_CRC;
        else sec0=SEC_PAD;
    end
    wire [12:0] filt_ra = pw-13'd3104;
    wire [12:0] ecg_ra  = pw-13'd3680;
    wire [12:0] ev_off  = pw-13'd4192;
    wire [12:0] spec_off= pw-13'd4256;
    reg [31:0] filt_q, ecg_q, ev_q, hdr_q, st_q; reg [127:0] spec_q;
    reg s1_v, s1_last, s1_evok, s1_specok; reg [3:0] s1_sec; reg [1:0] s1_wsel;
    reg issued_all;
    reg [31:0] fifo_d [0:3]; reg fifo_l [0:3]; reg [1:0] fifo_rd, fifo_wr; reg [2:0] fifo_cnt;
    wire issue_en = (state==S_OUTPUT) && !issued_all && ((fifo_cnt + s1_v) <= 3);
    always @(posedge clk) begin
        filt_q <= filt_mem[filt_ra[9:0]];
        ecg_q  <= ecg_mem[ecg_ra[8:0]];
        spec_q <= spectrum_mem[spec_off[9:2]];
        ev_q   <= ev_mem[ev_off[5:0]];
        hdr_q  <= header_word(pw[4:0]);
        st_q   <= status_word(pw[3:0]);
    end
    reg [31:0] d1;
    always_comb begin
        case(s1_sec)
            SEC_HDR:  d1=hdr_q;
            SEC_RAW:  d1=raw_q;
            SEC_FILT: d1=filt_q;
            SEC_ECG:  d1=ecg_q;
            SEC_EV:   d1=s1_evok ? ev_q : 32'd0;
            SEC_ST:   d1=st_q;
            SEC_SPEC: d1=s1_specok ? spec_q[s1_wsel*32+:32] : 32'd0;
            SEC_HLT:  d1=hlt_q;
            SEC_CRC:  d1=~crc_state;
            default:  d1=32'd0;
        endcase
    end
    wire fifo_push = s1_v;
    wire fifo_pop  = m_axis_tvalid && m_axis_tready;
    assign m_axis_tdata = fifo_d[fifo_rd];
    assign m_axis_tkeep = 4'hf;
    assign m_axis_tvalid = (state==S_OUTPUT) && (fifo_cnt!=0);
    assign m_axis_tlast = m_axis_tvalid && fifo_l[fifo_rd];
    assign s_axis_tready = (state==S_CAPTURE);

    // Common "frame finished" step: spectrum snapshot after the 128th raw frame, otherwise take the next frame.
    task automatic frame_done;
        begin
            if(frame_index==RAW_FRAMES-1)begin
                snapshot_target<=bin_count;
                snapshot_index<=0;
                packet_fft_start<=bin_count?bin_start_mem[bin_read]:32'hffffffff;
                packet_fft_end<=bin_count?bin_end_mem[bin_read]:32'hffffffff;
                state<=S_SNAPSHOT;
            end else begin frame_index<=frame_index+1'b1;state<=S_CAPTURE;end
        end
    endtask

    task automatic start_output;
        begin
            state<=S_OUTPUT;pw<=0;crc_state<=32'hffffffff;issued_all<=0;s1_v<=0;
            fifo_cnt<=0;fifo_rd<=0;fifo_wr<=0;
        end
    endtask

    wire [15:0] dec_clip_next = dec_clip_acc | ({15'd0,rail(raw_q[23:0])} << dch);

    // ECG record writer (one RAM write per clock, 4 clocks per record)
    reg [2:0] ecg_wr_cnt=0; reg [6:0] ecg_wr_frame=0; reg [31:0] rec_seq=0, rec_d=0, rec_y=0, rec_f=0;

    always @(posedge clk or negedge rst_n) begin
        if(!rst_n)begin
            state<=S_CAPTURE;frame_index<=0;word_index<=0;ra_ch<=0;ecg_ph<=0;filter_output_count<=0;
            d_started<=0;dec_issue<=0;dv<=0;dch<=0;ecg_a<=0;ecg_b<=0;ecg_wr_cnt<=0;
            frame_sequence<=0;expected_sequence<=0;sequence_initialized<=0;packet_first_sequence<=0;
            packet_first_cycle<=0;packet_last_cycle<=0;
            packet_sequence<=0;packet_clipped_frames<=0;packet_flags<=0;frame_clip_mask<=0;
            dec_clip_acc<=0;dec_clip_lat<=0;ecg_clip_lat<=0;ev_count<=0;ecg_mode_lat<=0;
            window_end_sequence<=0;window_generation<=0;snapshot_target<=0;snapshot_index<=0;
            packet_spectrum_count<=0;packet_fft_start<=32'hffffffff;packet_fft_end<=32'hffffffff;
            pw<=0;crc_state<=32'hffffffff;input_error<=0;fft_window_overrun<=0;issued_all<=0;s1_v<=0;
            s1_last<=0;s1_evok<=0;s1_specok<=0;s1_sec<=0;s1_wsel<=0;fifo_cnt<=0;fifo_rd<=0;fifo_wr<=0;
            filter_clipped_sticky<=0;reset_epoch<=0;packets_emitted<=0;
        end else if(flush)begin
            state<=S_CAPTURE;frame_index<=0;word_index<=0;ra_ch<=0;ecg_ph<=0;filter_output_count<=0;
            d_started<=0;dec_issue<=0;dv<=0;ecg_wr_cnt<=0;
            sequence_initialized<=0;packet_first_sequence<=0;packet_sequence<=0;
            packet_first_cycle<=0;packet_last_cycle<=0;
            packet_clipped_frames<=0;packet_flags<=32'h8;frame_clip_mask<=0;
            dec_clip_acc<=0;dec_clip_lat<=0;ecg_clip_lat<=0;ev_count<=0;ecg_mode_lat<=mode_ecg;
            window_end_sequence<=0;window_generation<=0;snapshot_target<=0;snapshot_index<=0;
            packet_spectrum_count<=0;packet_fft_start<=32'hffffffff;packet_fft_end<=32'hffffffff;
            pw<=0;crc_state<=32'hffffffff;input_error<=0;fft_window_overrun<=0;issued_all<=0;s1_v<=0;
            fifo_cnt<=0;fifo_rd<=0;fifo_wr<=0;
            filter_clipped_sticky<=0;reset_epoch<=reset_epoch+1'b1;packets_emitted<=0;
        end else begin
            if(fft_core_overrun)fft_window_overrun<=1;
            if(ecg_ev_valid && ev_count<4'd8)ev_count<=ev_count+1'b1;
            if(ecg_ev_valid && ev_count>=4'd8)packet_flags<=packet_flags|32'h200;
            if(dec_out_valid && !dec_out_warm)packet_flags<=packet_flags|32'h80;
            if(ecg_wr_cnt!=0)ecg_wr_cnt<=ecg_wr_cnt-1'b1;
            case(state)
                S_CAPTURE:if(s_axis_tvalid&&s_axis_tready)begin
                    if(parser_bad)begin input_error<=1;state<=S_FAULT;end
                    else begin
                        if(word_index==2)begin
                            frame_sequence<=s_axis_tdata;
                            if(!sequence_initialized)sequence_initialized<=1;
                            if(frame_index==0)packet_first_sequence<=s_axis_tdata;
                        end
                        if(word_index==0)begin
                            if(frame_index==0)packet_first_cycle<=s_axis_frame_cycle;
                            if(frame_index==RAW_FRAMES-1)packet_last_cycle<=s_axis_frame_cycle;
                        end
                        if(word_index==23)begin
                            expected_sequence<=frame_sequence+1'b1;
                            ra_ch<=0;ecg_ph<=0;d_started<=0;word_index<=0;
                            if(frame_index==0)ecg_mode_lat<=mode_ecg;
                            if(!mode_ecg)begin                                   // EEG16: zero ECG record, same writer
                                rec_seq<=frame_sequence;rec_d<=0;rec_y<=0;rec_f<=0;
                                ecg_wr_frame<=frame_index;ecg_wr_cnt<=3'd4;
                            end
                            state<=mode_ecg?S_ECG:S_DEC;
                        end else word_index<=word_index+1'b1;
                    end
                end
                S_ECG:begin
                    // ph0: address word 6 applied; ph1: raw_q=word 6, address word 7; ph2: raw_q=word 7; ph3: fire
                    case(ecg_ph)
                        2'd0:ecg_ph<=2'd1;
                        2'd1:begin ecg_a<=raw_q;ecg_ph<=2'd2;end
                        2'd2:begin ecg_b<=raw_q;ecg_ph<=2'd3;end
                        default:if(ecg_in_ready)begin ecg_clip_lat<=ecg_rail;state<=S_ECGW;end
                    endcase
                end
                S_ECGW:if(ecg_out_valid)begin
                    rec_seq<=frame_sequence;rec_d<={{7{ecg_out_diff[24]}},ecg_out_diff};rec_y<=ecg_out_y;
                    rec_f<={30'd0,1'b1,ecg_clip_lat};ecg_wr_frame<=frame_index;ecg_wr_cnt<=3'd4;
                    ra_ch<=0;d_started<=0;state<=S_DEC;
                end
                S_DEC:begin
                    dv<=dec_issue;dch<=ra_ch;
                    if(!d_started)begin
                        if(dec_in_ready)begin d_started<=1;dec_issue<=1;ra_ch<=0;end
                    end else if(dec_issue)begin
                        if(ra_ch==15)dec_issue<=0;else ra_ch<=ra_ch+1'b1;
                    end
                    if(dv)begin
                        if(!dec_in_ready)begin input_error<=1;state<=S_FAULT;end    // cannot happen: burst is atomic
                        dec_clip_acc<=dec_clip_next;
                        if(dch==15)begin
                            d_started<=0;ra_ch<=0;dv<=0;
                            if(frame_index[1:0]==2'd3)begin
                                dec_clip_lat<=dec_clip_next;dec_clip_acc<=16'd0;
                                filter_output_count<=0;frame_clip_mask<=0;
                                state<=S_FILT_WAIT;
                            end else frame_done();
                        end
                    end
                end
                S_FILT_WAIT:begin end
                S_SNAPSHOT:if(HEALTH!=0 && !health_done)begin end else begin
                    if(snapshot_index<snapshot_target)begin
                        if(!bin_pop)begin
                            packet_spectrum_count<=snapshot_index;
                            start_output();
                        end else begin
                            snapshot_index<=snapshot_index+1'b1;
                            if(snapshot_index+1'b1==snapshot_target)begin
                                packet_spectrum_count<=snapshot_target;
                                start_output();
                            end
                        end
                    end else begin packet_spectrum_count<=0;start_output();end
                end
                S_OUTPUT:begin
                    // stage 0: issue the next word
                    if(issue_en)begin
                        pw<=pw+1'b1;
                        s1_v<=1;s1_sec<=sec0;s1_last<=(pw==PACKET_WORDS-1);s1_wsel<=spec_off[1:0];
                        s1_evok<=((ev_off/6)<{9'd0,ev_count}) && (ev_off<13'd48);
                        s1_specok<=(spec_off[9:2]<packet_spectrum_count);
                        if(pw==PACKET_WORDS-1)issued_all<=1;
                    end else s1_v<=0;
                    // stage 1 -> FIFO, CRC over words 0..5279
                    if(s1_v)begin
                        fifo_d[fifo_wr]<=d1;fifo_l[fifo_wr]<=s1_last;fifo_wr<=fifo_wr+1'b1;
                        if(s1_sec!=SEC_CRC && s1_sec!=SEC_PAD)crc_state<=crc32_word(crc_state,d1);
                    end
                    fifo_cnt<=fifo_cnt+(fifo_push?3'd1:3'd0)-(fifo_pop?3'd1:3'd0);
                    if(fifo_pop)fifo_rd<=fifo_rd+1'b1;
                    if(fifo_pop && fifo_l[fifo_rd])begin
                        state<=S_CAPTURE;frame_index<=0;packet_sequence<=packet_sequence+1'b1;
                        packets_emitted<=packets_emitted+1'b1;packet_clipped_frames<=0;packet_flags<=0;
                        ev_count<=0;
                    end
                end
                S_FAULT:begin end
                default:begin state<=S_FAULT;input_error<=1;end
            endcase

            if(filter_out_valid && state!=S_FAULT)begin
                if(filter_out_clipped)begin
                    frame_clip_mask<=frame_clip_mask|(32'd1<<filter_out_channel);
                    filter_clipped_sticky<=1;
                end
                filter_output_count<=filter_output_count+1'b1;
                if(filter_output_count==15)begin
                    if(filter_out_channel!=15)begin input_error<=1;state<=S_FAULT;end
                    else begin
                        if(dseq[10:0]==11'd2047)begin
                            window_end_sequence<=dseq;
                            if(fft_state!=F_IDLE || window_generation!=fft_seen_generation)begin
                                fft_window_overrun<=1;packet_flags<=packet_flags|32'h40;
                            end
                            window_generation<=window_generation+1'b1;
                        end
                        if(frame_clip_mask!=0 || filter_out_clipped)
                            packet_clipped_frames<=packet_clipped_frames+1'b1;
                        frame_done();
                    end
                end
            end
        end
    end

    // ------------------------------------------------------------------ RAM write ports (single write per clock each)
    wire [9:0] fbase = {frame_index[6:2],4'b0000} + {4'b0000,frame_index[6:2],1'b0};     // frame*18
    reg clip_wr; reg [9:0] clip_wr_addr; reg [31:0] clip_wr_data;
    reg seq_wr;
    always @(posedge clk)begin
        if(rst_n && !flush)begin
            if(state==S_CAPTURE && s_axis_tvalid && s_axis_tready && !parser_bad)
                raw_mem[{5'd0,frame_index}*12'd24+{7'd0,word_index}]<=s_axis_tdata;
            // filtered RAM: samples directly, {dseq} when the 4th raw frame is handed over, {clip mask} one clock after ch 15
            if(filter_out_valid && state!=S_FAULT)
                filt_mem[fbase+10'd2+{6'd0,filter_out_channel}]<={{8{filter_out_sample[23]}},filter_out_sample};
            else if(clip_wr)
                filt_mem[clip_wr_addr]<=clip_wr_data;
            else if(state==S_DEC && dv && dch==15 && frame_index[1:0]==2'd3)
                filt_mem[fbase]<=dseq;
            // ECG record writer
            if(ecg_wr_cnt!=0)
                ecg_mem[{ecg_wr_frame,2'b00}+{7'd0,(3'd4-ecg_wr_cnt)}]<=
                    (ecg_wr_cnt==3'd4)?rec_seq:(ecg_wr_cnt==3'd3)?rec_d:(ecg_wr_cnt==3'd2)?rec_y:rec_f;
            if(ecg_ev_valid && ev_count<4'd8)begin
                ev_mem[ev_count*6+0]<=frame_sequence-32'd1;
                ev_mem[ev_count*6+1]<=ecg_ev_idx;
                ev_mem[ev_count*6+2]<=ecg_ev_pk[31:0];
                ev_mem[ev_count*6+3]<=ecg_ev_thr[31:0];
                ev_mem[ev_count*6+4]<={24'd0,ecg_ev_thr[35:32],ecg_ev_pk[35:32]};
                ev_mem[ev_count*6+5]<=32'd0;
            end
            if(bin_pop)spectrum_mem[snapshot_index]<=
                {32'd0,bin_imag_mem[bin_read],bin_real_mem[bin_read],
                 12'd0,bin_channel_mem[bin_read],bin_index_mem[bin_read]};
            if(bin_push)begin
                bin_index_mem[bin_write]<=fft_bin_index;
                bin_channel_mem[bin_write]<=fft_bin_channel;
                bin_real_mem[bin_write]<=fft_bin_real;
                bin_imag_mem[bin_write]<=fft_bin_imag;
                bin_start_mem[bin_write]<=fft_active_end-32'd2047;
                bin_end_mem[bin_write]<=fft_active_end;
            end
        end
    end
    always @(posedge clk or negedge rst_n)begin
        if(!rst_n)begin clip_wr<=0;clip_wr_addr<=0;clip_wr_data<=0;end
        else if(flush)clip_wr<=0;
        else begin
            clip_wr<=0;
            if(filter_out_valid && state!=S_FAULT && filter_output_count==15)begin
                clip_wr<=1;clip_wr_addr<=fbase+10'd1;
                clip_wr_data<=frame_clip_mask|(filter_out_clipped?(32'd1<<filter_out_channel):32'd0);
            end
        end
    end
    genvar bank;
    generate for(bank=0;bank<((FFT16!=0)?16:8);bank=bank+1)begin:fft_sample_banks
        (* ram_style="block" *) reg signed [23:0] samples[0:4095];
        reg signed [23:0] sample_read;
        always @(posedge clk)begin
            if(filter_out_valid && state!=S_FAULT && filter_out_channel==((bank+8)%16))
                samples[dseq[11:0]]<=filter_out_sample;
            if(fft_state==F_RING_READ)sample_read<=samples[ring_read_addr];
        end
        assign fft_ring_bank_q[bank]=sample_read;
    end endgenerate
    assign tap_valid = dec_out_valid;
    assign tap_ch = dec_out_channel;
    assign tap_x = dec_out_sample;
    assign tap_sat = dec_out_sat;
    assign tap_seq = dseq;
endmodule
