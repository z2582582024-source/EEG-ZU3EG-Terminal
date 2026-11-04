`timescale 1ns/1ps

// Synthesizable AXI4-Lite traffic source for offline verification only.
// cmd_addr is a peripheral-relative offset.  No system base address, PS
// Block Design, DMA, interrupt, or physical MMIO mapping is represented here.
module milestone_4d_fake_ps_axi_master #(
    parameter integer ADDR_WIDTH = 8
) (
    input  wire                  aclk,
    input  wire                  aresetn,

    input  wire                  cmd_valid,
    output wire                  cmd_ready,
    input  wire                  cmd_write,
    input  wire [ADDR_WIDTH-1:0] cmd_addr,
    input  wire [31:0]           cmd_wdata,
    input  wire [3:0]            cmd_wstrb,
    output reg                   cmd_done,
    output reg                   cmd_error,
    output reg  [31:0]           cmd_rdata,

    output reg  [ADDR_WIDTH-1:0] m_axi_awaddr,
    output reg                   m_axi_awvalid,
    input  wire                  m_axi_awready,
    output reg  [31:0]           m_axi_wdata,
    output reg  [3:0]            m_axi_wstrb,
    output reg                   m_axi_wvalid,
    input  wire                  m_axi_wready,
    input  wire [1:0]            m_axi_bresp,
    input  wire                  m_axi_bvalid,
    output reg                   m_axi_bready,
    output reg  [ADDR_WIDTH-1:0] m_axi_araddr,
    output reg                   m_axi_arvalid,
    input  wire                  m_axi_arready,
    input  wire [31:0]           m_axi_rdata,
    input  wire [1:0]            m_axi_rresp,
    input  wire                  m_axi_rvalid,
    output reg                   m_axi_rready
);
    localparam [2:0] ST_IDLE       = 3'd0;
    localparam [2:0] ST_WRITE_SEND = 3'd1;
    localparam [2:0] ST_WRITE_RESP = 3'd2;
    localparam [2:0] ST_READ_SEND  = 3'd3;
    localparam [2:0] ST_READ_RESP  = 3'd4;

    reg [2:0] state;
    reg aw_pending;
    reg w_pending;

    assign cmd_ready = state == ST_IDLE;

    always @(posedge aclk or negedge aresetn) begin
        if (!aresetn) begin
            state <= ST_IDLE;
            aw_pending <= 1'b0;
            w_pending <= 1'b0;
            cmd_done <= 1'b0;
            cmd_error <= 1'b0;
            cmd_rdata <= 32'd0;
            m_axi_awaddr <= {ADDR_WIDTH{1'b0}};
            m_axi_awvalid <= 1'b0;
            m_axi_wdata <= 32'd0;
            m_axi_wstrb <= 4'd0;
            m_axi_wvalid <= 1'b0;
            m_axi_bready <= 1'b0;
            m_axi_araddr <= {ADDR_WIDTH{1'b0}};
            m_axi_arvalid <= 1'b0;
            m_axi_rready <= 1'b0;
        end else begin
            cmd_done <= 1'b0;
            case (state)
                ST_IDLE: begin
                    cmd_error <= 1'b0;
                    m_axi_bready <= 1'b0;
                    m_axi_rready <= 1'b0;
                    if (cmd_valid) begin
                        if (cmd_write) begin
                            m_axi_awaddr <= cmd_addr;
                            m_axi_awvalid <= 1'b1;
                            m_axi_wdata <= cmd_wdata;
                            m_axi_wstrb <= cmd_wstrb;
                            m_axi_wvalid <= 1'b1;
                            aw_pending <= 1'b1;
                            w_pending <= 1'b1;
                            state <= ST_WRITE_SEND;
                        end else begin
                            m_axi_araddr <= cmd_addr;
                            m_axi_arvalid <= 1'b1;
                            state <= ST_READ_SEND;
                        end
                    end
                end

                ST_WRITE_SEND: begin
                    if (m_axi_awvalid && m_axi_awready) begin
                        m_axi_awvalid <= 1'b0;
                        aw_pending <= 1'b0;
                    end
                    if (m_axi_wvalid && m_axi_wready) begin
                        m_axi_wvalid <= 1'b0;
                        w_pending <= 1'b0;
                    end
                    if ((!aw_pending || (m_axi_awvalid && m_axi_awready)) &&
                        (!w_pending || (m_axi_wvalid && m_axi_wready))) begin
                        m_axi_bready <= 1'b1;
                        state <= ST_WRITE_RESP;
                    end
                end

                ST_WRITE_RESP: begin
                    if (m_axi_bvalid && m_axi_bready) begin
                        cmd_error <= m_axi_bresp != 2'b00;
                        cmd_done <= 1'b1;
                        m_axi_bready <= 1'b0;
                        state <= ST_IDLE;
                    end
                end

                ST_READ_SEND: begin
                    if (m_axi_arvalid && m_axi_arready) begin
                        m_axi_arvalid <= 1'b0;
                        m_axi_rready <= 1'b1;
                        state <= ST_READ_RESP;
                    end
                end

                ST_READ_RESP: begin
                    if (m_axi_rvalid && m_axi_rready) begin
                        cmd_rdata <= m_axi_rdata;
                        cmd_error <= m_axi_rresp != 2'b00;
                        cmd_done <= 1'b1;
                        m_axi_rready <= 1'b0;
                        state <= ST_IDLE;
                    end
                end

                default: begin
                    state <= ST_IDLE;
                    aw_pending <= 1'b0;
                    w_pending <= 1'b0;
                    m_axi_awvalid <= 1'b0;
                    m_axi_wvalid <= 1'b0;
                    m_axi_bready <= 1'b0;
                    m_axi_arvalid <= 1'b0;
                    m_axi_rready <= 1'b0;
                    cmd_error <= 1'b1;
                    cmd_done <= 1'b1;
                end
            endcase
        end
    end
endmodule
