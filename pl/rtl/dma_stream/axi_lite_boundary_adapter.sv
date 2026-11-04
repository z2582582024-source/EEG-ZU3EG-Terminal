`timescale 1ns/1ps
// One independent read and write outstanding. Local 4 KiB address aperture;
// offsets outside the unchanged 512-byte core and unaligned offsets fail closed.
// AWPROT/ARPROT are metadata only: no privilege/security enforcement is claimed.
module axi_lite_boundary_adapter(
    input wire clk, input wire rst_n,
    input wire [11:0] s_awaddr, input wire [2:0] s_awprot,
    input wire s_awvalid, output wire s_awready,
    input wire [31:0] s_wdata, input wire [3:0] s_wstrb,
    input wire s_wvalid, output wire s_wready,
    output wire [1:0] s_bresp, output wire s_bvalid, input wire s_bready,
    input wire [11:0] s_araddr, input wire [2:0] s_arprot,
    input wire s_arvalid, output wire s_arready,
    output wire [31:0] s_rdata, output wire [1:0] s_rresp,
    output wire s_rvalid, input wire s_rready,
    output wire [8:0] m_awaddr, output wire m_awvalid, input wire m_awready,
    output wire [31:0] m_wdata, output wire [3:0] m_wstrb,
    output wire m_wvalid, input wire m_wready,
    input wire [1:0] m_bresp, input wire m_bvalid, output wire m_bready,
    output wire [8:0] m_araddr, output wire m_arvalid, input wire m_arready,
    input wire [31:0] m_rdata, input wire [1:0] m_rresp,
    input wire m_rvalid, output wire m_rready
);
    localparam [1:0] COLLECT=0, SEND=1, WAIT_REPLY=2, RESPOND=3;
    reg [1:0] wstate, rstate;
    reg awheld, wheld, awsent, wsent;
    reg [11:0] awaddr, araddr;
    reg [31:0] wdata, rdata;
    reg [3:0] wstrb;
    reg [1:0] bresp, rresp;
    wire write_address_ok=(awaddr[11:9]==0)&&(awaddr[1:0]==0);
    wire read_address_ok=(s_araddr[11:9]==0)&&(s_araddr[1:0]==0);
    assign s_awready=rst_n&&(wstate==COLLECT)&&!awheld;
    assign s_wready=rst_n&&(wstate==COLLECT)&&!wheld;
    assign s_bvalid=rst_n&&(wstate==RESPOND);
    assign s_bresp=bresp;
    assign m_awaddr=awaddr[8:0];
    assign m_awvalid=rst_n&&(wstate==SEND)&&!awsent;
    assign m_wdata=wdata; assign m_wstrb=wstrb;
    assign m_wvalid=rst_n&&(wstate==SEND)&&!wsent;
    assign m_bready=rst_n&&(wstate==WAIT_REPLY);
    assign s_arready=rst_n&&(rstate==COLLECT);
    assign s_rvalid=rst_n&&(rstate==RESPOND);
    assign s_rdata=rdata; assign s_rresp=rresp;
    assign m_araddr=araddr[8:0];
    assign m_arvalid=rst_n&&(rstate==SEND);
    assign m_rready=rst_n&&(rstate==WAIT_REPLY);
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            wstate<=COLLECT; awheld<=0; wheld<=0; awsent<=0; wsent<=0;
            awaddr<=0; wdata<=0; wstrb<=0; bresp<=0;
        end else case(wstate)
            COLLECT:begin
                if(s_awvalid&&s_awready)begin awaddr<=s_awaddr; awheld<=1;end
                if(s_wvalid&&s_wready)begin wdata<=s_wdata; wstrb<=s_wstrb; wheld<=1;end
                if(awheld&&wheld)begin
                    awsent<=0; wsent<=0;
                    if(write_address_ok)wstate<=SEND;
                    else begin bresp<=2'b10;wstate<=RESPOND;end
                end
            end
            SEND:begin
                if(m_awvalid&&m_awready)awsent<=1;
                if(m_wvalid&&m_wready)wsent<=1;
                if((awsent||m_awready)&&(wsent||m_wready))wstate<=WAIT_REPLY;
            end
            WAIT_REPLY:if(m_bvalid&&m_bready)begin bresp<=m_bresp;wstate<=RESPOND;end
            RESPOND:if(s_bready)begin awheld<=0;wheld<=0;wstate<=COLLECT;end
        endcase
    end
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n)begin rstate<=COLLECT;araddr<=0;rdata<=0;rresp<=0;end
        else case(rstate)
            COLLECT:if(s_arvalid&&s_arready)begin
                araddr<=s_araddr;
                if(read_address_ok)rstate<=SEND;
                else begin rdata<=0;rresp<=2'b10;rstate<=RESPOND;end
            end
            SEND:if(m_arready)rstate<=WAIT_REPLY;
            WAIT_REPLY:if(m_rvalid&&m_rready)begin rdata<=m_rdata;rresp<=m_rresp;rstate<=RESPOND;end
            RESPOND:if(s_rready)rstate<=COLLECT;
        endcase
    end
endmodule
