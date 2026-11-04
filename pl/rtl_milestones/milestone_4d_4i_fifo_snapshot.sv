`timescale 1ns/1ps
module milestone_4d_4i_fifo_snapshot (
    input wire clk, input wire rst_n,
    input wire snapshot_req, input wire consume_ack_req, input wire fault_req,
    input wire head_valid, input wire [31:0] head_sequence,
    input wire [215:0] head_ads1_frame, input wire [215:0] head_ads2_frame,
    input wire [23:0] head_ads1_status, input wire [23:0] head_ads2_status,
    output reg fifo_consume_req, output reg snapshot_valid,
    output reg [31:0] snapshot_token, output reg [31:0] snapshot_sequence,
    output reg [215:0] snapshot_ads1_frame, output reg [215:0] snapshot_ads2_frame,
    output reg [23:0] snapshot_ads1_status, output reg [23:0] snapshot_ads2_status,
    output reg [31:0] token_error_count, output reg token_error_sticky
);
    function automatic [31:0] sat_inc(input [31:0] value);
        sat_inc=(value==32'hFFFF_FFFF)?value:value+1'b1;
    endfunction
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            fifo_consume_req<=0; snapshot_valid<=0; snapshot_token<=0;
            snapshot_sequence<=0; snapshot_ads1_frame<=0; snapshot_ads2_frame<=0;
            snapshot_ads1_status<=0; snapshot_ads2_status<=0;
            token_error_count<=0; token_error_sticky<=0;
        end else begin
            fifo_consume_req<=0;
            if(fault_req) snapshot_valid<=0;
            if(snapshot_req && !fault_req) begin
                snapshot_token<=snapshot_token+1'b1;
                snapshot_valid<=head_valid;
                if(head_valid) begin
                    snapshot_sequence<=head_sequence;
                    snapshot_ads1_frame<=head_ads1_frame;
                    snapshot_ads2_frame<=head_ads2_frame;
                    snapshot_ads1_status<=head_ads1_status;
                    snapshot_ads2_status<=head_ads2_status;
                end
            end
            if(consume_ack_req && !fault_req) begin
                if(snapshot_valid && head_valid && head_sequence==snapshot_sequence) begin
                    fifo_consume_req<=1'b1;
                    snapshot_valid<=1'b0;
                end else begin
                    token_error_count<=sat_inc(token_error_count);
                    token_error_sticky<=1'b1;
                end
            end
        end
    end
endmodule
