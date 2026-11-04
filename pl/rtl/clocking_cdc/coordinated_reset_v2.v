`timescale 1ns/1ps
// Both domains must observe clocks after common assertion before either releases.
// Not a running-clock detector: a later stopped clock cannot assert reset here.
module coordinated_reset_v2(
    input wire axi_clk,acq_clk,
    input wire axi_resetn_request,acq_resetn_request,
    output wire axi_rst_n,acq_rst_n
);
    wire common_resetn=axi_resetn_request&&acq_resetn_request;
    (* ASYNC_REG="TRUE" *) reg [2:0] axi_local,acq_local;
    (* ASYNC_REG="TRUE" *) reg [2:0] axi_peer,acq_peer;
    always @(posedge axi_clk or negedge common_resetn)
        if(!common_resetn)begin axi_local<=0;axi_peer<=0;end
        else begin axi_local<={axi_local[1:0],1'b1};axi_peer<={axi_peer[1:0],acq_local[2]};end
    always @(posedge acq_clk or negedge common_resetn)
        if(!common_resetn)begin acq_local<=0;acq_peer<=0;end
        else begin acq_local<={acq_local[1:0],1'b1};acq_peer<={acq_peer[1:0],axi_local[2]};end
    assign axi_rst_n=axi_local[2]&&axi_peer[2];
    assign acq_rst_n=acq_local[2]&&acq_peer[2];
endmodule
