`timescale 1ns/1ps
module milestone_4d_4i_axi_cdc #(
    parameter integer STATUS_WIDTH=2016
) (
    input wire axi_clk,input wire axi_rst_n,input wire acq_clk,input wire acq_rst_n,
    input wire arm_request_toggle,input wire shutdown_request_toggle,
    input wire clear_request_toggle,input wire snapshot_request_toggle,
    input wire fifo_snapshot_request_toggle,input wire consume_request_toggle,
    input wire fault_request_toggle,input wire [STATUS_WIDTH-1:0] acq_status_live,
    output wire acq_arm_pulse,output wire acq_shutdown_pulse,
    output wire acq_clear_pulse,output wire arm_ack_toggle,
    output wire shutdown_ack_toggle,output wire clear_ack_toggle,
    output wire snapshot_ack_toggle,output wire [STATUS_WIDTH-1:0] axi_status_snapshot,
    output reg acq_fifo_snapshot_pulse,output reg acq_consume_pulse,
    output reg acq_fault_pulse,output reg fifo_snapshot_ack_toggle,
    output reg consume_ack_toggle,output reg fault_ack_toggle
);
    milestone_4d_axi_cdc #(.STATUS_WIDTH(STATUS_WIDTH)) base_cdc(
        .axi_clk(axi_clk),.axi_rst_n(axi_rst_n),.acq_clk(acq_clk),.acq_rst_n(acq_rst_n),
        .arm_request_toggle(arm_request_toggle),.shutdown_request_toggle(shutdown_request_toggle),
        .clear_request_toggle(clear_request_toggle),.snapshot_request_toggle(snapshot_request_toggle),
        .acq_status_live(acq_status_live),.acq_arm_pulse(acq_arm_pulse),
        .acq_shutdown_pulse(acq_shutdown_pulse),.acq_clear_pulse(acq_clear_pulse),
        .arm_ack_toggle(arm_ack_toggle),.shutdown_ack_toggle(shutdown_ack_toggle),
        .clear_ack_toggle(clear_ack_toggle),.snapshot_ack_toggle(snapshot_ack_toggle),
        .axi_status_snapshot(axi_status_snapshot));
    (* ASYNC_REG="TRUE" *) reg [1:0] fs_sync,consume_sync,fault_sync;
    reg fs_ack_acq,consume_ack_acq,fault_ack_acq;
    (* ASYNC_REG="TRUE" *) reg [1:0] fs_ack_sync,consume_ack_sync,fault_ack_sync;
    always @(posedge acq_clk or negedge acq_rst_n) begin
        if(!acq_rst_n) begin fs_sync<=0;consume_sync<=0;fault_sync<=0;
            fs_ack_acq<=0;consume_ack_acq<=0;fault_ack_acq<=0;
            acq_fifo_snapshot_pulse<=0;acq_consume_pulse<=0;acq_fault_pulse<=0;end
        else begin
            fs_sync<={fs_sync[0],fifo_snapshot_request_toggle};
            consume_sync<={consume_sync[0],consume_request_toggle};
            fault_sync<={fault_sync[0],fault_request_toggle};
            acq_fifo_snapshot_pulse<=0;acq_consume_pulse<=0;acq_fault_pulse<=0;
            if(fs_sync[1]!=fs_ack_acq) begin acq_fifo_snapshot_pulse<=1;fs_ack_acq<=fs_sync[1];end
            if(consume_sync[1]!=consume_ack_acq) begin acq_consume_pulse<=1;consume_ack_acq<=consume_sync[1];end
            if(fault_sync[1]!=fault_ack_acq) begin acq_fault_pulse<=1;fault_ack_acq<=fault_sync[1];end
        end
    end
    always @(posedge axi_clk or negedge axi_rst_n) begin
        if(!axi_rst_n) begin fs_ack_sync<=0;consume_ack_sync<=0;fault_ack_sync<=0;
            fifo_snapshot_ack_toggle<=0;consume_ack_toggle<=0;fault_ack_toggle<=0;end
        else begin
            fs_ack_sync<={fs_ack_sync[0],fs_ack_acq};
            consume_ack_sync<={consume_ack_sync[0],consume_ack_acq};
            fault_ack_sync<={fault_ack_sync[0],fault_ack_acq};
            fifo_snapshot_ack_toggle<=fs_ack_sync[1];
            consume_ack_toggle<=consume_ack_sync[1];fault_ack_toggle<=fault_ack_sync[1];
        end
    end
endmodule
