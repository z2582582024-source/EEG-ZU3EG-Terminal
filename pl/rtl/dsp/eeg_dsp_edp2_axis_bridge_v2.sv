`timescale 1ns/1ps

module eeg_dsp_edp2_axis_bridge_v2 #(
    parameter TWIDDLE_INIT_FILE="twiddle_q15_v2.hex",
    parameter HANN_INIT_FILE="hann_periodic_q15_v2.hex"
)(
    input wire clk,
    input wire rst_n,
    input wire flush,
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
    output reg [31:0] packets_emitted
);
    localparam [2:0] S_CAPTURE=3'd0, S_FILTER_FEED=3'd1, S_FILTER_DRAIN=3'd2,
                     S_SNAPSHOT=3'd3, S_OUTPUT=3'd4, S_FAULT=3'd5;
    localparam [2:0] F_IDLE=3'd0, F_RING_READ=3'd1, F_LOAD=3'd2, F_WAIT=3'd3, F_DRAIN=3'd4;
    reg [2:0] state=S_CAPTURE;
    reg [2:0] fft_state=F_IDLE;

    reg [31:0] raw_mem[0:767];
    reg [31:0] filtered_sequence_mem[0:31];
    reg [31:0] filtered_clip_mem[0:31];
    reg [31:0] filtered_sample_mem[0:511];
    reg [127:0] spectrum_mem[0:255];
    wire signed [23:0] fft_ring_bank_q[0:7];
    reg [15:0] bin_index_mem[0:255];
    reg [3:0] bin_channel_mem[0:255];
    reg signed [31:0] bin_real_mem[0:255];
    reg signed [31:0] bin_imag_mem[0:255];
    reg [31:0] bin_start_mem[0:255];
    reg [31:0] bin_end_mem[0:255];

    reg [4:0] frame_index=0;
    reg [4:0] word_index=0;
    reg [3:0] filter_channel=0;
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
    reg [11:0] output_word=0;
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

    wire [11:0] ring_read_addr={fft_active_end[11],fft_load_index};
    assign fft_ring_sample=fft_ring_bank_q[fft_channel-4'd8];
    eeg_fft2048_serial_v2 #(.TWIDDLE_INIT_FILE(TWIDDLE_INIT_FILE),.HANN_INIT_FILE(HANN_INIT_FILE)) fft_core(
        .clk(clk),.rst_n(rst_n),.flush(flush),
        .in_valid(fft_state==F_LOAD && fft_core_ready),.in_channel(fft_channel),.in_sample(fft_ring_sample),
        .in_ready(fft_core_ready),.busy(fft_core_busy),.input_overrun(fft_core_overrun),
        .bin_valid(fft_bin_valid),.bin_ready(fft_bin_ready),.bin_index(fft_bin_index),
        .bin_channel(fft_bin_channel),.bin_real(fft_bin_real),.bin_imag(fft_bin_imag));

    wire filter_in_valid=(state==S_FILTER_FEED);
    wire [31:0] raw_filter_word=(filter_channel<8)?
        raw_mem[frame_index*24+6+filter_channel]:
        raw_mem[frame_index*24+14+(filter_channel-8)];
    wire raw_at_rail=(raw_filter_word[23:0]==24'h7fffff)||(raw_filter_word[23:0]==24'h800000);
    wire parser_bad=(s_axis_tkeep!=4'hf) ||
        (s_axis_tlast!=((frame_index==31)&&(word_index==23))) ||
        (word_index==0 && s_axis_tdata!=32'h31444d45) ||
        (word_index==1 && s_axis_tdata!=32'h00600001) ||
        (word_index==2 && sequence_initialized && s_axis_tdata!=expected_sequence) ||
        (word_index==3 && s_axis_tdata!=frame_sequence) ||
        ((word_index==4 || word_index==5) && s_axis_tdata[23:20]!=4'hc) ||
        (word_index==22 && s_axis_tdata!=32'd4096000) ||
        (word_index==23 && s_axis_tdata!=32'h00000103);
    wire filter_out_valid,filter_out_clipped;
    wire [3:0] filter_out_channel;
    wire signed [23:0] filter_out_sample;
    eeg_dsp_filter16_q30_v2 filter16(
        .clk(clk),.rst_n(rst_n),.flush(flush || state==S_FAULT),
        .in_valid(filter_in_valid),.in_clipped(raw_at_rail),.in_channel(filter_channel),
        .in_sample(raw_filter_word[23:0]),.out_valid(filter_out_valid),
        .out_clipped(filter_out_clipped),.out_channel(filter_out_channel),.out_sample(filter_out_sample));

    wire bin_push=(fft_state==F_DRAIN)&&fft_bin_valid&&fft_bin_ready;
    wire bin_pop=(state==S_SNAPSHOT)&&(snapshot_index<snapshot_target)&&
        (bin_start_mem[bin_read]==packet_fft_start)&&(bin_end_mem[bin_read]==packet_fft_end);
    assign fft_bin_ready=(fft_state==F_DRAIN)&&(bin_count<9'd256);
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n)begin bin_count<=0;bin_write<=0;bin_read<=0;end
        else if(flush)begin bin_count<=0;bin_write<=0;bin_read<=0;end
        else begin
            if(bin_push)begin
                bin_write<=bin_write+1'b1;
            end
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
            F_RING_READ:begin
                fft_state<=F_LOAD;
            end
            F_LOAD:if(fft_core_ready)begin
                    if(fft_load_index==11'd2047)begin fft_load_index<=0;fft_state<=F_WAIT;end
                else begin fft_load_index<=fft_load_index+1'b1;fft_state<=F_RING_READ;end
                end
                F_WAIT:if(fft_bin_valid)fft_state<=F_DRAIN;
                F_DRAIN:if(bin_push && fft_bin_index==11'd1024)begin
                    if(fft_channel<15)begin fft_channel<=fft_channel+1'b1;fft_load_index<=0;fft_state<=F_RING_READ;end
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
                0:header_word=32'h32504445;
                1:header_word=32'h00800002;
                2:header_word=32'd9664;
                3:header_word=32'h00000003 | ((packet_spectrum_count!=0)?32'h4:0) |
                    (packet_clipped_frames?32'h20:0) | (packet_flags&32'h58);
                4:header_word=packet_sequence;
                5:header_word=packet_first_sequence;
                6:header_word=32'd32;
                7:header_word=32'd4096000;
                8:header_word=32'd3072;
                9:header_word=32'd2304;
                10:header_word=packet_spectrum_count;
                11:header_word=32'd256;
                12:header_word=32'd2048;
                13:header_word=packet_fft_start;
                14:header_word=packet_fft_end;
                15:header_word=32'h0000ff00;
                16:header_word=32'd16;
                17:header_word=32'h00020001;
                18:header_word=32'h00010001;
                19:header_word=32'd1;
                20:header_word=32'd1;
                21:header_word=32'd2048;
                22:header_word=32'd1000000000;
                23:header_word=32'd4096000;
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

    reg [31:0] output_data;
    always @* begin
        output_data=0;
        if(output_word<32)output_data=header_word(output_word[4:0]);
        else if(output_word<800)output_data=raw_mem[output_word-32];
        else if(output_word<1376)begin
            if((output_word-800)%18==0)output_data=filtered_sequence_mem[(output_word-800)/18];
            else if((output_word-800)%18==1)output_data=filtered_clip_mem[(output_word-800)/18];
            else output_data=filtered_sample_mem[((output_word-800)/18)*16+((output_word-800)%18)-2];
        end
        else if(output_word<2400)begin
            if(((output_word-1376)>>2)<packet_spectrum_count)
                output_data=spectrum_mem[(output_word-1376)>>2][((output_word-1376)%4)*32+:32];
            else output_data=0;
        end
        else if(output_word==2400)output_data=~crc_state;
    end
    assign m_axis_tdata=output_data;
    assign m_axis_tkeep=4'hf;
    assign m_axis_tvalid=(state==S_OUTPUT);
    assign m_axis_tlast=(state==S_OUTPUT)&&(output_word==12'd2415);
    assign s_axis_tready=(state==S_CAPTURE);

    always @(posedge clk or negedge rst_n) begin
        if(!rst_n)begin
            state<=S_CAPTURE;frame_index<=0;word_index<=0;filter_channel<=0;filter_output_count<=0;
            frame_sequence<=0;expected_sequence<=0;sequence_initialized<=0;packet_first_sequence<=0;
            packet_first_cycle<=0;packet_last_cycle<=0;
            packet_sequence<=0;packet_clipped_frames<=0;packet_flags<=0;frame_clip_mask<=0;
            window_end_sequence<=0;window_generation<=0;snapshot_target<=0;snapshot_index<=0;
            packet_spectrum_count<=0;packet_fft_start<=32'hffffffff;packet_fft_end<=32'hffffffff;
            output_word<=0;crc_state<=32'hffffffff;input_error<=0;fft_window_overrun<=0;
            filter_clipped_sticky<=0;reset_epoch<=0;packets_emitted<=0;
        end else if(flush)begin
            state<=S_CAPTURE;frame_index<=0;word_index<=0;filter_channel<=0;filter_output_count<=0;
            sequence_initialized<=0;packet_first_sequence<=0;packet_sequence<=0;
            packet_first_cycle<=0;packet_last_cycle<=0;
            packet_clipped_frames<=0;packet_flags<=32'h8;frame_clip_mask<=0;
            window_end_sequence<=0;window_generation<=0;snapshot_target<=0;snapshot_index<=0;
            packet_spectrum_count<=0;packet_fft_start<=32'hffffffff;packet_fft_end<=32'hffffffff;
            output_word<=0;crc_state<=32'hffffffff;input_error<=0;fft_window_overrun<=0;
            filter_clipped_sticky<=0;reset_epoch<=reset_epoch+1'b1;packets_emitted<=0;
        end else begin
            if(fft_core_overrun)fft_window_overrun<=1;
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
                            if(frame_index==31)packet_last_cycle<=s_axis_frame_cycle;
                        end
                        if(word_index==23)begin
                            expected_sequence<=frame_sequence+1'b1;
                            filter_channel<=0;filter_output_count<=0;frame_clip_mask<=0;
                            state<=S_FILTER_FEED;word_index<=0;
                        end else word_index<=word_index+1'b1;
                    end
                end
                S_FILTER_FEED:begin
                    if(filter_channel==15)state<=S_FILTER_DRAIN;
                    else filter_channel<=filter_channel+1'b1;
                end
                S_FILTER_DRAIN:begin end
                S_SNAPSHOT:begin
                    if(snapshot_index<snapshot_target)begin
                        if(!bin_pop)begin
                            packet_spectrum_count<=snapshot_index;
                            state<=S_OUTPUT;output_word<=0;crc_state<=32'hffffffff;
                        end else begin
                            snapshot_index<=snapshot_index+1'b1;
                            if(snapshot_index+1'b1==snapshot_target)begin
                                packet_spectrum_count<=snapshot_target;
                                state<=S_OUTPUT;output_word<=0;crc_state<=32'hffffffff;
                            end
                        end
                    end else begin packet_spectrum_count<=0;state<=S_OUTPUT;output_word<=0;crc_state<=32'hffffffff;end
                end
                S_OUTPUT:if(m_axis_tvalid&&m_axis_tready)begin
                    if(output_word<2400)crc_state<=crc32_word(crc_state,output_data);
                    if(output_word==12'd2415)begin
                        state<=S_CAPTURE;frame_index<=0;packet_sequence<=packet_sequence+1'b1;
                        packets_emitted<=packets_emitted+1'b1;packet_clipped_frames<=0;packet_flags<=0;
                    end else output_word<=output_word+1'b1;
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
                    else if(frame_sequence[10:0]==11'd2047)begin
                        window_end_sequence<=frame_sequence;
                        if(fft_state!=F_IDLE || window_generation!=fft_seen_generation)begin
                            fft_window_overrun<=1;packet_flags<=packet_flags|32'h40;
                        end
                        window_generation<=window_generation+1'b1;
                    end
                    if(frame_clip_mask!=0 || filter_out_clipped)
                        packet_clipped_frames<=packet_clipped_frames+1'b1;
                    if(frame_index==31)begin
                        snapshot_target<=bin_count;
                        snapshot_index<=0;
                        packet_fft_start<=bin_count?bin_start_mem[bin_read]:32'hffffffff;
                        packet_fft_end<=bin_count?bin_end_mem[bin_read]:32'hffffffff;
                        state<=S_SNAPSHOT;
                    end else begin frame_index<=frame_index+1'b1;state<=S_CAPTURE;end
                end
            end
        end
    end
    // Single-write inference templates have no array reset. Their addresses
    // are protected by the reset FSM/count/sequence state; stale cells cannot
    // become valid packets. This avoids expanding the payload RAM into FFs.
    always @(posedge clk)begin
        if(rst_n && !flush)begin
            if(state==S_CAPTURE && s_axis_tvalid && s_axis_tready && !parser_bad)begin
                raw_mem[frame_index*24+word_index]<=s_axis_tdata;
                if(word_index==23)filtered_sequence_mem[frame_index]<=frame_sequence;
            end
            if(filter_out_valid && state!=S_FAULT)begin
                filtered_sample_mem[{frame_index,filter_out_channel}]<=
                    {{8{filter_out_sample[23]}},filter_out_sample};
                if(filter_output_count==15)
                    filtered_clip_mem[frame_index]<=frame_clip_mask|
                        (filter_out_clipped?(32'd1<<filter_out_channel):32'd0);
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
    // Synchronous independent read port per bank; each 4096 x 24 channel ring
    // maps to BRAM while streaming writes continue throughout FFT transforms.
    genvar bank;
    generate for(bank=0;bank<8;bank=bank+1)begin:fft_sample_banks
        (* ram_style="block" *) reg signed [23:0] samples[0:4095];
        reg signed [23:0] sample_read;
        always @(posedge clk)begin
            if(filter_out_valid && state!=S_FAULT && filter_out_channel==bank+8)
                samples[frame_sequence[11:0]]<=filter_out_sample;
            if(fft_state==F_RING_READ)sample_read<=samples[ring_read_addr];
        end
        assign fft_ring_bank_q[bank]=sample_read;
    end endgenerate
endmodule
