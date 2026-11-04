`timescale 1ns/1ps

// Self-contained AXI4-Lite control/status peripheral.  All addresses are
// offsets relative to the slave; no Zynq system base address is assigned.
module milestone_4d_axi_lite_regs #(
    parameter integer ADDR_WIDTH = 8,
    parameter integer STATUS_WIDTH = 1088
) (
    input  wire                    s_axi_aclk,
    input  wire                    s_axi_aresetn,
    input  wire [ADDR_WIDTH-1:0]   s_axi_awaddr,
    input  wire                    s_axi_awvalid,
    output wire                    s_axi_awready,
    input  wire [31:0]             s_axi_wdata,
    input  wire [3:0]              s_axi_wstrb,
    input  wire                    s_axi_wvalid,
    output wire                    s_axi_wready,
    output reg  [1:0]              s_axi_bresp,
    output reg                     s_axi_bvalid,
    input  wire                    s_axi_bready,
    input  wire [ADDR_WIDTH-1:0]   s_axi_araddr,
    input  wire                    s_axi_arvalid,
    output wire                    s_axi_arready,
    output reg  [31:0]             s_axi_rdata,
    output reg  [1:0]              s_axi_rresp,
    output reg                     s_axi_rvalid,
    input  wire                    s_axi_rready,
    input  wire [STATUS_WIDTH-1:0] status_snapshot,
    input  wire                    arm_ack_toggle,
    input  wire                    shutdown_ack_toggle,
    input  wire                    clear_ack_toggle,
    input  wire                    snapshot_ack_toggle,
    output reg                     arm_request_toggle,
    output reg                     shutdown_request_toggle,
    output reg                     clear_request_toggle,
    output reg                     snapshot_request_toggle
);
    localparam [7:0] REG_CONTROL          = 8'h00;
    localparam [7:0] REG_STATUS           = 8'h04;
    localparam [7:0] REG_ERROR_FLAGS      = 8'h08;
    localparam [7:0] REG_ADS1_IDS         = 8'h0C;
    localparam [7:0] REG_ADS2_IDS         = 8'h10;
    localparam [7:0] REG_ADS1_CFG_PASS    = 8'h14;
    localparam [7:0] REG_ADS2_CFG_PASS    = 8'h18;
    localparam [7:0] REG_COMPLETE_PAIRS   = 8'h1C;
    localparam [7:0] REG_DISCARDED        = 8'h20;
    localparam [7:0] REG_PARTIAL          = 8'h24;
    localparam [7:0] REG_DRDY_ERRORS      = 8'h28;
    localparam [7:0] REG_SPI_ERRORS       = 8'h2C;
    localparam [7:0] REG_ADS1_STATUS      = 8'h30;
    localparam [7:0] REG_ADS2_STATUS      = 8'h34;
    localparam [7:0] REG_WINDOW_SUMMARY   = 8'h38;
    localparam [7:0] REG_WDOG_REASON      = 8'h3C;
    localparam [7:0] REG_ADS1_CFG0        = 8'h40;
    localparam [7:0] REG_ADS1_CFG1        = 8'h44;
    localparam [7:0] REG_ADS1_CFG2        = 8'h48;
    localparam [7:0] REG_ADS2_CFG0        = 8'h4C;
    localparam [7:0] REG_ADS2_CFG1        = 8'h50;
    localparam [7:0] REG_ADS2_CFG2        = 8'h54;
    localparam [7:0] REG_TOTAL_DRDY        = 8'h58;
    localparam [7:0] REG_FRAME_TIMEOUTS    = 8'h5C;
    localparam [7:0] REG_FRAME_LENGTH_ERR  = 8'h60;
    localparam [7:0] REG_STATUS_HEADER_ERR = 8'h64;
    localparam [7:0] REG_DETAIL_STATUS     = 8'h68;
    localparam [7:0] REG_EVENT_STATUS      = 8'h6C;
    localparam [7:0] REG_CDC_STATUS        = 8'h70;
    localparam [7:0] REG_CAPABILITY        = 8'h74;
    localparam [7:0] REG_WATCHDOG_COUNT    = 8'h78;
    localparam [7:0] REG_ADS1_CHANNEL0     = 8'h80;
    localparam [7:0] REG_ADS1_CHANNEL5     = 8'h94;
    localparam [7:0] REG_ADS2_CHANNEL0     = 8'h98;
    localparam [7:0] REG_ADS2_CHANNEL5     = 8'hAC;

    reg aw_hold_valid;
    reg [ADDR_WIDTH-1:0] aw_hold_addr;
    reg w_hold_valid;
    reg [31:0] w_hold_data;
    reg [3:0] w_hold_strb;
    wire write_fire = aw_hold_valid && w_hold_valid && !s_axi_bvalid;
    wire [7:0] write_offset = {aw_hold_addr[7:2], 2'b00};
    wire [7:0] read_offset = {s_axi_araddr[7:2], 2'b00};

    // Snapshot bit allocation is frozen by milestone_4d_production_offline_top:
    //  4:0 state, 5 busy, 6 id gate, 7 config, 8 window active,
    //  9 shutdown safe, 10 fault safe, 26:11 error flags,
    //  50:27 ADS1 IDs, 74:51 ADS2 IDs, 85:75/96:86 cfg pass,
    //  128:97 complete, 160:129 discarded, 192:161 partial,
    //  224:193 DRDY errors, 256:225 SPI errors,
    //  280:257/304:281 last status, 312:305 window summary,
    //  344:313 watchdog, 352:345 reason, 440:353/528:441 cfg readback,
    //  560:529 total DRDY, 592:561 frame timeout,
    //  624:593 frame length, 656:625 header errors,
    //  672:657 valid positions, 676:673 config status,
    //  677 boundary safe, 679:678 accepted count, 680 shutdown seen.
    // 872:681 ADS1 last channels, 1064:873 ADS2 last channels.
    // STATUS_WIDTH is 1088 in the production integration.

    assign s_axi_awready = !aw_hold_valid && !s_axi_bvalid;
    assign s_axi_wready  = !w_hold_valid && !s_axi_bvalid;
    assign s_axi_arready = !s_axi_rvalid;

    function automatic is_readable(input [7:0] addr);
        begin
            case (addr)
                REG_CONTROL, REG_STATUS, REG_ERROR_FLAGS, REG_ADS1_IDS,
                REG_ADS2_IDS, REG_ADS1_CFG_PASS, REG_ADS2_CFG_PASS,
                REG_COMPLETE_PAIRS, REG_DISCARDED, REG_PARTIAL,
                REG_DRDY_ERRORS, REG_SPI_ERRORS, REG_ADS1_STATUS,
                REG_ADS2_STATUS, REG_WINDOW_SUMMARY, REG_WDOG_REASON,
                REG_ADS1_CFG0, REG_ADS1_CFG1, REG_ADS1_CFG2,
                REG_ADS2_CFG0, REG_ADS2_CFG1, REG_ADS2_CFG2,
                REG_TOTAL_DRDY, REG_FRAME_TIMEOUTS, REG_FRAME_LENGTH_ERR,
                REG_STATUS_HEADER_ERR, REG_DETAIL_STATUS, REG_EVENT_STATUS,
                REG_CDC_STATUS, REG_CAPABILITY, REG_WATCHDOG_COUNT:
                    is_readable = 1'b1;
                default: is_readable =
                    (addr >= REG_ADS1_CHANNEL0 && addr <= REG_ADS1_CHANNEL5) ||
                    (addr >= REG_ADS2_CHANNEL0 && addr <= REG_ADS2_CHANNEL5);
            endcase
        end
    endfunction

    function automatic [31:0] read_mux(input [7:0] addr);
        begin
            case (addr)
                REG_CONTROL: read_mux = {28'd0,
                    snapshot_request_toggle != snapshot_ack_toggle,
                    clear_request_toggle != clear_ack_toggle,
                    shutdown_request_toggle != shutdown_ack_toggle,
                    arm_request_toggle != arm_ack_toggle};
                REG_STATUS: read_mux = {21'd0, status_snapshot[10:0]};
                REG_ERROR_FLAGS: read_mux = {16'd0, status_snapshot[26:11]};
                REG_ADS1_IDS: read_mux = {8'd0, status_snapshot[50:27]};
                REG_ADS2_IDS: read_mux = {8'd0, status_snapshot[74:51]};
                REG_ADS1_CFG_PASS: read_mux = {21'd0, status_snapshot[85:75]};
                REG_ADS2_CFG_PASS: read_mux = {21'd0, status_snapshot[96:86]};
                REG_COMPLETE_PAIRS: read_mux = status_snapshot[128:97];
                REG_DISCARDED: read_mux = status_snapshot[160:129];
                REG_PARTIAL: read_mux = status_snapshot[192:161];
                REG_DRDY_ERRORS: read_mux = status_snapshot[224:193];
                REG_SPI_ERRORS: read_mux = status_snapshot[256:225];
                REG_ADS1_STATUS: read_mux = {8'd0, status_snapshot[280:257]};
                REG_ADS2_STATUS: read_mux = {8'd0, status_snapshot[304:281]};
                REG_WINDOW_SUMMARY: read_mux = {24'd0, status_snapshot[312:305]};
                // Low 24 watchdog bits plus the 8-bit shutdown reason.
                REG_WDOG_REASON: read_mux = {status_snapshot[336:313],
                                             status_snapshot[352:345]};
                REG_ADS1_CFG0: read_mux = status_snapshot[384:353];
                REG_ADS1_CFG1: read_mux = status_snapshot[416:385];
                REG_ADS1_CFG2: read_mux = {8'd0, status_snapshot[440:417]};
                REG_ADS2_CFG0: read_mux = status_snapshot[472:441];
                REG_ADS2_CFG1: read_mux = status_snapshot[504:473];
                REG_ADS2_CFG2: read_mux = {8'd0, status_snapshot[528:505]};
                REG_TOTAL_DRDY: read_mux = status_snapshot[560:529];
                REG_FRAME_TIMEOUTS: read_mux = status_snapshot[592:561];
                REG_FRAME_LENGTH_ERR: read_mux = status_snapshot[624:593];
                REG_STATUS_HEADER_ERR: read_mux = status_snapshot[656:625];
                REG_DETAIL_STATUS: read_mux = {10'd0, status_snapshot[677:657]};
                REG_EVENT_STATUS: read_mux = {29'd0, status_snapshot[680:678]};
                REG_CDC_STATUS: read_mux = {24'd0,
                    snapshot_ack_toggle, snapshot_request_toggle,
                    clear_ack_toggle, clear_request_toggle,
                    shutdown_ack_toggle, shutdown_request_toggle,
                    arm_ack_toggle, arm_request_toggle};
                REG_CAPABILITY: read_mux = 32'h4D44_0100;
                REG_WATCHDOG_COUNT: read_mux = status_snapshot[344:313];
                default: begin
                    if (addr >= REG_ADS1_CHANNEL0 && addr <= REG_ADS1_CHANNEL5)
                        read_mux = status_snapshot[681 +
                            (((addr - REG_ADS1_CHANNEL0) >> 2) * 32) +: 32];
                    else if (addr >= REG_ADS2_CHANNEL0 && addr <= REG_ADS2_CHANNEL5)
                        read_mux = status_snapshot[873 +
                            (((addr - REG_ADS2_CHANNEL0) >> 2) * 32) +: 32];
                    else read_mux = 32'd0;
                end
            endcase
        end
    endfunction

    always @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if (!s_axi_aresetn) begin
            aw_hold_valid <= 1'b0;
            aw_hold_addr <= {ADDR_WIDTH{1'b0}};
            w_hold_valid <= 1'b0;
            w_hold_data <= 32'd0;
            w_hold_strb <= 4'd0;
            s_axi_bresp <= 2'b00;
            s_axi_bvalid <= 1'b0;
            s_axi_rdata <= 32'd0;
            s_axi_rresp <= 2'b00;
            s_axi_rvalid <= 1'b0;
            arm_request_toggle <= 1'b0;
            shutdown_request_toggle <= 1'b0;
            clear_request_toggle <= 1'b0;
            snapshot_request_toggle <= 1'b0;
        end else begin
            if (s_axi_awready && s_axi_awvalid) begin
                aw_hold_valid <= 1'b1;
                aw_hold_addr <= s_axi_awaddr;
            end
            if (s_axi_wready && s_axi_wvalid) begin
                w_hold_valid <= 1'b1;
                w_hold_data <= s_axi_wdata;
                w_hold_strb <= s_axi_wstrb;
            end
            if (write_fire) begin
                aw_hold_valid <= 1'b0;
                w_hold_valid <= 1'b0;
                s_axi_bvalid <= 1'b1;
                if (write_offset == REG_CONTROL) begin
                    s_axi_bresp <= 2'b00;
                    if (w_hold_strb[0]) begin
                        if (w_hold_data[0] && arm_request_toggle == arm_ack_toggle)
                            arm_request_toggle <= ~arm_request_toggle;
                        if (w_hold_data[1] && shutdown_request_toggle == shutdown_ack_toggle)
                            shutdown_request_toggle <= ~shutdown_request_toggle;
                        if (w_hold_data[2] && clear_request_toggle == clear_ack_toggle)
                            clear_request_toggle <= ~clear_request_toggle;
                        if (w_hold_data[3] && snapshot_request_toggle == snapshot_ack_toggle)
                            snapshot_request_toggle <= ~snapshot_request_toggle;
                    end
                end else begin
                    // Every implemented non-CONTROL register is read only.
                    s_axi_bresp <= 2'b10;
                end
            end else if (s_axi_bvalid && s_axi_bready) begin
                s_axi_bvalid <= 1'b0;
            end

            if (s_axi_arready && s_axi_arvalid) begin
                s_axi_rvalid <= 1'b1;
                if (is_readable(read_offset)) begin
                    s_axi_rdata <= read_mux(read_offset);
                    s_axi_rresp <= 2'b00;
                end else begin
                    s_axi_rdata <= 32'd0;
                    s_axi_rresp <= 2'b10;
                end
            end else if (s_axi_rvalid && s_axi_rready) begin
                s_axi_rvalid <= 1'b0;
            end
        end
    end
endmodule
