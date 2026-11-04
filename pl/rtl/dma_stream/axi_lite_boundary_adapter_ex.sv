`timescale 1ns/1ps
// AXI-Lite boundary adapter with an extension window.  Same behaviour as verification/milestone4j4k_ps_axi_candidate/axi_lite_boundary_adapter.sv
// for byte offsets 0x000..0x1FF (forwarded to the unchanged 9-bit core register map); byte offsets 0x200..0xFFF are forwarded to the
// extension port x_* (the SSVEP unit register window).  One independent read and one independent write outstanding.  Unaligned offsets fail
// closed (SLVERR).  The extension port protocol: x_wen / x_ren are single-clock strobes with the 12-bit byte offset; the unit answers a write
// with a single-clock x_wack (x_werr marks an error) and a read with a single-clock x_rvalid carrying x_rdata (x_rerr marks an error).
module axi_lite_boundary_adapter_ex(
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
    input wire m_rvalid, output wire m_rready,
    // extension window
    output reg x_wen, output reg [11:0] x_waddr, output reg [31:0] x_wdata,
    input wire x_wack, input wire x_werr,
    output reg x_ren, output reg [11:0] x_raddr,
    input wire x_rvalid, input wire [31:0] x_rdata, input wire x_rerr
);
    localparam [2:0] COLLECT=0, SEND=1, WAIT_REPLY=2, RESPOND=3, XSEND=4, XWAIT=5;
    reg [2:0] wstate, rstate;
    reg awheld, wheld, awsent, wsent;
    reg [11:0] awaddr, araddr;
    reg [31:0] wdata, rdata;
    reg [3:0] wstrb;
    reg [1:0] bresp, rresp;
    wire w_ext = (awaddr[11:9] != 3'd0);
    wire r_ext = (araddr[11:9] != 3'd0);
    wire write_address_ok = (awaddr[1:0] == 2'd0);
    wire read_address_ok = (s_araddr[1:0] == 2'd0);
    assign s_awready = rst_n && (wstate == COLLECT) && !awheld;
    assign s_wready = rst_n && (wstate == COLLECT) && !wheld;
    assign s_bvalid = rst_n && (wstate == RESPOND);
    assign s_bresp = bresp;
    assign m_awaddr = awaddr[8:0];
    assign m_awvalid = rst_n && (wstate == SEND) && !awsent;
    assign m_wdata = wdata; assign m_wstrb = wstrb;
    assign m_wvalid = rst_n && (wstate == SEND) && !wsent;
    assign m_bready = rst_n && (wstate == WAIT_REPLY);
    assign s_arready = rst_n && (rstate == COLLECT);
    assign s_rvalid = rst_n && (rstate == RESPOND);
    assign s_rdata = rdata; assign s_rresp = rresp;
    assign m_araddr = araddr[8:0];
    assign m_arvalid = rst_n && (rstate == SEND);
    assign m_rready = rst_n && (rstate == WAIT_REPLY);
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wstate <= COLLECT; awheld <= 0; wheld <= 0; awsent <= 0; wsent <= 0;
            awaddr <= 0; wdata <= 0; wstrb <= 0; bresp <= 0; x_wen <= 0; x_waddr <= 0; x_wdata <= 0;
        end else begin
            x_wen <= 1'b0;
            case (wstate)
            COLLECT: begin
                if (s_awvalid && s_awready) begin awaddr <= s_awaddr; awheld <= 1; end
                if (s_wvalid && s_wready) begin wdata <= s_wdata; wstrb <= s_wstrb; wheld <= 1; end
                if (awheld && wheld) begin
                    awsent <= 0; wsent <= 0;
                    if (!write_address_ok) begin bresp <= 2'b10; wstate <= RESPOND; end
                    else if (w_ext) begin
                        if (wstrb != 4'hf) begin bresp <= 2'b10; wstate <= RESPOND; end
                        else begin x_wen <= 1'b1; x_waddr <= awaddr; x_wdata <= wdata; wstate <= XWAIT; end
                    end else wstate <= SEND;
                end
            end
            SEND: begin
                if (m_awvalid && m_awready) awsent <= 1;
                if (m_wvalid && m_wready) wsent <= 1;
                if ((awsent || m_awready) && (wsent || m_wready)) wstate <= WAIT_REPLY;
            end
            WAIT_REPLY: if (m_bvalid && m_bready) begin bresp <= m_bresp; wstate <= RESPOND; end
            XWAIT: if (x_wack) begin bresp <= x_werr ? 2'b10 : 2'b00; wstate <= RESPOND; end
            RESPOND: if (s_bready) begin awheld <= 0; wheld <= 0; wstate <= COLLECT; end
            default: wstate <= COLLECT;
            endcase
        end
    end
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin rstate <= COLLECT; araddr <= 0; rdata <= 0; rresp <= 0; x_ren <= 0; x_raddr <= 0; end
        else begin
            x_ren <= 1'b0;
            case (rstate)
            COLLECT: if (s_arvalid && s_arready) begin
                araddr <= s_araddr;
                if (!read_address_ok) begin rdata <= 0; rresp <= 2'b10; rstate <= RESPOND; end
                else if (s_araddr[11:9] != 3'd0) begin x_ren <= 1'b1; x_raddr <= s_araddr; rstate <= XWAIT; end
                else rstate <= SEND;
            end
            SEND: if (m_arready) rstate <= WAIT_REPLY;
            WAIT_REPLY: if (m_rvalid && m_rready) begin rdata <= m_rdata; rresp <= m_rresp; rstate <= RESPOND; end
            XWAIT: if (x_rvalid) begin rdata <= x_rerr ? 32'd0 : x_rdata; rresp <= x_rerr ? 2'b10 : 2'b00; rstate <= RESPOND; end
            RESPOND: if (s_rready) rstate <= COLLECT;
            default: rstate <= COLLECT;
            endcase
        end
    end
endmodule
