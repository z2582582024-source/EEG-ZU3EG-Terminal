`timescale 1ns/1ps
// Continuous 32-frame AXIS packets until explicit stop/lease fault. No finite256 cap. No ADC backpressure. A whole frame is
// atomically latched AND popped before serialization; only this owner may pop.
// Contract: acq_clk == axi_clk == DMA clocks, coordinated reset, little endian.
module axis_cont_packetizer_terminal_v1 #(
    parameter integer FRAME_LIMIT=0,
    parameter [31:0] SAMPLE_INTERVAL_NS=32'd4096000   // design interval written to word 22 (4096000: 244.140625 SPS, 1024000: 976.5625 SPS)
)(
    input wire clk, input wire rst_n, input wire prepared,
    input wire fault_req, input wire head_valid,
    input wire [31:0] head_sequence,
    input wire [63:0] head_frame_cycle,output reg [63:0] m_axis_frame_cycle,
    input wire [215:0] head_ads1_frame, head_ads2_frame,
    output wire fifo_pop,
    output reg [31:0] m_axis_tdata,
    output wire [3:0] m_axis_tkeep,
    output wire m_axis_tvalid, input wire m_axis_tready,
    output wire m_axis_tlast,
    output reg [31:0] frames_sent, output reg [31:0] stall_cycles,
    output wire [31:0] stream_status
);
    reg busy, aborted;
    reg [4:0] word_index;
    reg [31:0] sequence_latched;
    reg [215:0] frame1, frame2;
    wire done = FRAME_LIMIT != 0 && frames_sent == FRAME_LIMIT;
    assign fifo_pop = prepared && !busy && !done && !aborted &&
                      !fault_req && head_valid;
    assign m_axis_tvalid = busy;
    assign m_axis_tkeep = 4'hf;
    assign m_axis_tlast = busy && word_index == 23 && frames_sent[4:0] == 31;
    assign stream_status = {27'd0,aborted,done,busy,prepared,1'b1};
    reg [23:0] raw;
    always @* begin
        raw=0;
        case(word_index)
            0: m_axis_tdata=32'h31444d45; // bytes E M D 1
            1: m_axis_tdata=32'h00600001; // 96 bytes, version 1
            2: m_axis_tdata=sequence_latched;
            3: m_axis_tdata=frames_sent;
            4: m_axis_tdata={8'd0,frame1[215:192]};
            5: m_axis_tdata={8'd0,frame2[215:192]};
            22: m_axis_tdata=SAMPLE_INTERVAL_NS; // DESIGN interval, not measured time
            23: m_axis_tdata=32'h00000103; // EXT1 external configuration + design time; not proof of source
            default: begin
                if(word_index>=6 && word_index<=13)
                    raw=frame1[191-(word_index-6)*24 -:24];
                else if(word_index>=14 && word_index<=21)
                    raw=frame2[191-(word_index-14)*24 -:24];
                m_axis_tdata={{8{raw[23]}},raw};
            end
        endcase
    end
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            m_axis_frame_cycle<=0;busy<=0;aborted<=0;word_index<=0;sequence_latched<=0;
            frame1<=0;frame2<=0;frames_sent<=0;stall_cycles<=0;
        end else begin
            if(fault_req) aborted<=1;
            // A fault cannot withdraw TVALID or change a stalled word. Finish
            // the owned frame if accepted; never acquire another frame.
            if(busy && !m_axis_tready && stall_cycles!=32'hffffffff)
                stall_cycles<=stall_cycles+1;
            if(fifo_pop) begin
                m_axis_frame_cycle<=head_frame_cycle;frame1<=head_ads1_frame;frame2<=head_ads2_frame;
                sequence_latched<=head_sequence;word_index<=0;busy<=1;
            end else if(busy && m_axis_tready) begin
                if(word_index==23) begin
                    busy<=0;word_index<=0;frames_sent<=frames_sent+1;
                end else word_index<=word_index+1;
            end
        end
    end
endmodule
