`timescale 1ns/1ps
module cont_dma_regs_leadoff_terminal_v1 #(
    parameter integer ADDR_WIDTH=9, parameter integer STATUS_WIDTH=2152
) (
    input wire s_axi_aclk,input wire s_axi_aresetn,
    input wire [ADDR_WIDTH-1:0] s_axi_awaddr,input wire s_axi_awvalid,
    output wire s_axi_awready,input wire [31:0] s_axi_wdata,
    input wire [3:0] s_axi_wstrb,input wire s_axi_wvalid,output wire s_axi_wready,
    output reg [1:0] s_axi_bresp,output reg s_axi_bvalid,input wire s_axi_bready,
    input wire [ADDR_WIDTH-1:0] s_axi_araddr,input wire s_axi_arvalid,
    output wire s_axi_arready,output reg [31:0] s_axi_rdata,
    output reg [1:0] s_axi_rresp,output reg s_axi_rvalid,input wire s_axi_rready,
    output reg dma_prepared, output reg heartbeat_toggle,
    input wire [31:0] dma_frames,dma_stalls,dma_status,
    input wire [STATUS_WIDTH-1:0] status_snapshot,
    input wire arm_ack_toggle,input wire shutdown_ack_toggle,input wire clear_ack_toggle,
    input wire snapshot_ack_toggle,input wire fifo_snapshot_ack_toggle,
    input wire consume_ack_toggle,input wire fault_ack_toggle,
    input wire lead_off_busy,input wire lead_off_done,input wire lead_off_failed,input wire lead_off_unknown,
    input wire [31:0] lead_off_status_word,input wire [7:0] lead_off_error_code,
    input wire [31:0] lead_off_error_word,
    input wire [31:0] lead_off_sample_counts,input wire [31:0] lead_off_off_masks,
    input wire [31:0] lead_off_owner_state,input wire [31:0] lead_off_phase_word,
    input wire [31:0] initialize_status,output reg initialize_request,
    input wire initialize_admission_allowed,input wire arm_admission_allowed,
    output reg arm_request_toggle,output reg shutdown_request_toggle,
    output reg clear_request_toggle,output reg snapshot_request_toggle,
    output reg fifo_snapshot_request_toggle,output reg consume_request_toggle,
    output reg fault_request_toggle,
    output reg lead_off_start_request,output reg lead_off_cancel_request,
    output reg lead_off_acknowledge_request
);
    import milestone_4d_4i_register_pkg::*;
    reg aw_hold_valid,w_hold_valid;reg [ADDR_WIDTH-1:0] aw_hold_addr;
    reg [31:0] w_hold_data;reg [3:0] w_hold_strb;
    wire write_fire=aw_hold_valid&&w_hold_valid&&!s_axi_bvalid;
    wire [8:0] write_offset={aw_hold_addr[8:2],2'b00};
    wire [8:0] read_offset={s_axi_araddr[8:2],2'b00};
    assign s_axi_awready=!aw_hold_valid&&!s_axi_bvalid;
    assign s_axi_wready=!w_hold_valid&&!s_axi_bvalid;
    assign s_axi_arready=!s_axi_rvalid;

    function automatic is_readable(input [8:0] addr);
        begin
            case(addr)
                REG_CONTROL,REG_STATUS,REG_ERROR_FLAGS,REG_ADS1_IDS,REG_ADS2_IDS,
                REG_ADS1_CFG_PASS,REG_ADS2_CFG_PASS,REG_COMPLETE_PAIRS,REG_DISCARDED,
                REG_PARTIAL,REG_DRDY_ERRORS,REG_SPI_ERRORS,REG_ADS1_STATUS,REG_ADS2_STATUS,
                REG_WINDOW_SUMMARY,REG_WDOG_REASON,REG_ADS1_CFG0,REG_ADS1_CFG1,
                REG_ADS1_CFG2,REG_ADS2_CFG0,REG_ADS2_CFG1,REG_ADS2_CFG2,REG_TOTAL_DRDY,
                REG_FRAME_TIMEOUTS,REG_FRAME_LENGTH_ERR,REG_STATUS_HEADER_ERR,
                REG_DETAIL_STATUS,REG_EVENT_STATUS,REG_CDC_STATUS,REG_CAPABILITY,
                REG_WATCHDOG_COUNT,REG_FIFO_STATUS,REG_FIFO_OCCUPANCY,REG_FIFO_CAPACITY,
                REG_PRODUCER_SEQUENCE,REG_CONSUMER_SEQUENCE,REG_OVERFLOW_COUNT,
                REG_DROPPED_COUNT,REG_PARTIAL_PAIR_COUNT,REG_SEQUENCE_GAP_COUNT,
                REG_LAST_ACCEPTED_SEQUENCE,REG_LAST_CONSUMED_SEQUENCE,REG_SNAPSHOT_TOKEN,
                REG_FRAME_METADATA,REG_FRAME_ADS1_STATUS,REG_FRAME_ADS2_STATUS,
                9'h170,9'h164,9'h168,9'h16c,9'h140,9'h144,9'h148,9'h14c,9'h150,9'h154,9'h158,9'h15c,9'h160,
                REG_FIFO_CONTROL,9'h178,9'h17c,9'h184,9'h188,9'h18c,9'h190,9'h194,9'h198,9'h19c,
                9'h1a0,9'h1a4,9'h1a8,9'h1ac,9'h1b0:is_readable=1'b1;
                default:is_readable=((addr>=REG_ADS1_CHANNEL0&&addr<=9'h094)||
                    (addr>=REG_ADS2_CHANNEL0&&addr<=9'h0AC)||
                    (addr>=REG_FRAME_ADS1_WORD0&&addr<=9'h108)||
                    (addr>=REG_FRAME_ADS2_WORD0&&addr<=9'h128));
            endcase
        end
    endfunction
    function automatic [31:0] frame_channel(input bit adc2,input [2:0] index);
        reg [23:0] raw;integer base;
        begin
            base=adc2?UF_SNAPSHOT_ADS2_FRAME_LSB:UF_SNAPSHOT_ADS1_FRAME_LSB;
            raw=status_snapshot[base+191-index*24 -:24];
            frame_channel={{8{raw[23]}},raw};
        end
    endfunction
    function automatic [31:0] read_mux(input [8:0] addr);
        reg [2:0] index;
        begin
            case(addr)
                9'h140:read_mux=32'h444d0500;
                9'h164:read_mux=status_snapshot[2047:2016];
                9'h168:read_mux=status_snapshot[2079:2048];
                9'h16c:read_mux=32'h54455232;
                9'h178:read_mux=initialize_status;
                9'h17c:read_mux=32'h00020002;
                9'h1a0:read_mux=status_snapshot[2119:2088];
                9'h1a4:read_mux=status_snapshot[2151:2120];
                9'h1a8:read_mux=32'd2;
                9'h1ac:read_mux=32'd100000000;
                9'h1b0:read_mux=32'd21904;
                9'h144:read_mux={31'd0,dma_prepared};
                9'h170:read_mux={31'd0,heartbeat_toggle};
                9'h148:read_mux=dma_frames;
                9'h14c:read_mux=dma_status;
                9'h150:read_mux=dma_stalls;
                9'h154:read_mux=0;
                9'h158:read_mux=0;
                9'h15c:read_mux=32;
                9'h160:read_mux=16;
                REG_CONTROL:read_mux={28'd0,snapshot_request_toggle!=snapshot_ack_toggle,
                    clear_request_toggle!=clear_ack_toggle,shutdown_request_toggle!=shutdown_ack_toggle,
                    arm_request_toggle!=arm_ack_toggle};
                REG_STATUS:read_mux={21'd0,status_snapshot[10:0]};
                REG_ERROR_FLAGS:read_mux={16'd0,status_snapshot[26:11]};
                REG_ADS1_IDS:read_mux={8'd0,status_snapshot[50:27]};
                REG_ADS2_IDS:read_mux={8'd0,status_snapshot[74:51]};
                REG_ADS1_CFG_PASS:read_mux={17'd0,status_snapshot[2083:2080],status_snapshot[85:75]};
                REG_ADS2_CFG_PASS:read_mux={17'd0,status_snapshot[2087:2084],status_snapshot[96:86]};
                REG_COMPLETE_PAIRS:read_mux=status_snapshot[128:97];
                REG_DISCARDED:read_mux=status_snapshot[160:129];
                REG_PARTIAL:read_mux=status_snapshot[192:161];
                REG_DRDY_ERRORS:read_mux=status_snapshot[224:193];
                REG_SPI_ERRORS:read_mux=status_snapshot[256:225];
                REG_ADS1_STATUS:read_mux={8'd0,status_snapshot[280:257]};
                REG_ADS2_STATUS:read_mux={8'd0,status_snapshot[304:281]};
                REG_WINDOW_SUMMARY:read_mux={24'd0,status_snapshot[312:305]};
                REG_WDOG_REASON:read_mux={status_snapshot[336:313],status_snapshot[352:345]};
                REG_ADS1_CFG0:read_mux=status_snapshot[384:353];
                REG_ADS1_CFG1:read_mux=status_snapshot[416:385];
                REG_ADS1_CFG2:read_mux={8'd0,status_snapshot[440:417]};
                REG_ADS2_CFG0:read_mux=status_snapshot[472:441];
                REG_ADS2_CFG1:read_mux=status_snapshot[504:473];
                REG_ADS2_CFG2:read_mux={8'd0,status_snapshot[528:505]};
                REG_TOTAL_DRDY:read_mux=status_snapshot[560:529];
                REG_FRAME_TIMEOUTS:read_mux=status_snapshot[592:561];
                REG_FRAME_LENGTH_ERR:read_mux=status_snapshot[624:593];
                REG_STATUS_HEADER_ERR:read_mux=status_snapshot[656:625];
                REG_DETAIL_STATUS:read_mux={10'd0,status_snapshot[677:657]};
                REG_EVENT_STATUS:read_mux={29'd0,status_snapshot[680:678]};
                REG_CDC_STATUS:read_mux={18'd0,fault_ack_toggle,fault_request_toggle,
                    consume_ack_toggle,consume_request_toggle,fifo_snapshot_ack_toggle,
                    fifo_snapshot_request_toggle,snapshot_ack_toggle,snapshot_request_toggle,
                    clear_ack_toggle,clear_request_toggle,shutdown_ack_toggle,
                    shutdown_request_toggle,arm_ack_toggle,arm_request_toggle};
                REG_CAPABILITY:read_mux=32'h4D49_0100;
                9'h184:read_mux=lead_off_status_word;
                9'h188:read_mux={24'd0,lead_off_error_code};
                9'h18c:read_mux=lead_off_sample_counts;
                9'h190:read_mux=lead_off_off_masks;
                9'h194:read_mux=lead_off_owner_state;
                9'h198:read_mux=lead_off_error_word;
                9'h19c:read_mux=lead_off_phase_word;
                REG_WATCHDOG_COUNT:read_mux=status_snapshot[344:313];
                REG_FIFO_STATUS:read_mux={status_snapshot[UF_TOKEN_ERROR_LSB+:16],7'd0,
                    status_snapshot[UF_FIFO_STATUS_LSB+:9]};
                REG_FIFO_OCCUPANCY:read_mux=status_snapshot[UF_OCCUPANCY_LSB+:32];
                REG_FIFO_CAPACITY:read_mux=status_snapshot[UF_CAPACITY_LSB+:32];
                REG_PRODUCER_SEQUENCE:read_mux=status_snapshot[UF_PRODUCER_LSB+:32];
                REG_CONSUMER_SEQUENCE:read_mux=status_snapshot[UF_CONSUMER_LSB+:32];
                REG_OVERFLOW_COUNT:read_mux=status_snapshot[UF_OVERFLOW_LSB+:32];
                REG_DROPPED_COUNT:read_mux=status_snapshot[UF_DROPPED_LSB+:32];
                REG_PARTIAL_PAIR_COUNT:read_mux=status_snapshot[UF_PARTIAL_LSB+:32];
                REG_SEQUENCE_GAP_COUNT:read_mux=status_snapshot[UF_GAP_LSB+:32];
                REG_LAST_ACCEPTED_SEQUENCE:read_mux=status_snapshot[UF_LAST_ACCEPTED_LSB+:32];
                REG_LAST_CONSUMED_SEQUENCE:read_mux=status_snapshot[UF_LAST_CONSUMED_LSB+:32];
                REG_SNAPSHOT_TOKEN:read_mux=status_snapshot[UF_TOKEN_LSB+:32];
                REG_FRAME_METADATA:read_mux=status_snapshot[UF_SNAPSHOT_SEQUENCE_LSB+:32];
                REG_FRAME_ADS1_STATUS:read_mux={8'd0,status_snapshot[UF_SNAPSHOT_ADS1_STATUS_LSB+:24]};
                REG_FRAME_ADS2_STATUS:read_mux={8'd0,status_snapshot[UF_SNAPSHOT_ADS2_STATUS_LSB+:24]};
                REG_FIFO_CONTROL:read_mux={27'd0,clear_request_toggle!=clear_ack_toggle,
                    fault_request_toggle!=fault_ack_toggle,shutdown_request_toggle!=shutdown_ack_toggle,
                    consume_request_toggle!=consume_ack_toggle,
                    fifo_snapshot_request_toggle!=fifo_snapshot_ack_toggle};
                default:begin
                    if(addr>=REG_ADS1_CHANNEL0&&addr<=9'h094)
                        read_mux=status_snapshot[681+(((addr-REG_ADS1_CHANNEL0)>>2)*32)+:32];
                    else if(addr>=REG_ADS2_CHANNEL0&&addr<=9'h0AC)
                        read_mux=status_snapshot[873+(((addr-REG_ADS2_CHANNEL0)>>2)*32)+:32];
                    else if(addr>=REG_FRAME_ADS1_WORD0&&addr<=9'h108) begin
                        index=(addr-REG_FRAME_ADS1_WORD0)>>2;read_mux=frame_channel(1'b0,index);end
                    else if(addr>=REG_FRAME_ADS2_WORD0&&addr<=9'h128) begin
                        index=(addr-REG_FRAME_ADS2_WORD0)>>2;read_mux=frame_channel(1'b1,index);end
                    else read_mux=0;
                end
            endcase
        end
    endfunction
    always @(posedge s_axi_aclk or negedge s_axi_aresetn) begin
        if(!s_axi_aresetn) begin
            dma_prepared<=0;heartbeat_toggle<=0;
            aw_hold_valid<=0;w_hold_valid<=0;s_axi_bresp<=0;s_axi_bvalid<=0;
            s_axi_rdata<=0;s_axi_rresp<=0;s_axi_rvalid<=0;
            arm_request_toggle<=0;shutdown_request_toggle<=0;clear_request_toggle<=0;
            snapshot_request_toggle<=0;fifo_snapshot_request_toggle<=0;
            consume_request_toggle<=0;fault_request_toggle<=0;
            lead_off_start_request<=0;lead_off_cancel_request<=0;lead_off_acknowledge_request<=0;
            initialize_request<=0;
        end else begin
            initialize_request<=0;
            lead_off_start_request<=0;lead_off_cancel_request<=0;lead_off_acknowledge_request<=0;
            if(s_axi_awready&&s_axi_awvalid)begin aw_hold_valid<=1;aw_hold_addr<=s_axi_awaddr;end
            if(s_axi_wready&&s_axi_wvalid)begin w_hold_valid<=1;w_hold_data<=s_axi_wdata;w_hold_strb<=s_axi_wstrb;end
            if(write_fire) begin
                aw_hold_valid<=0;w_hold_valid<=0;s_axi_bvalid<=1;s_axi_bresp<=2'b00;
                if(write_offset==9'h174)begin
                    if(w_hold_strb!=4'hf || w_hold_data!=1 || !initialize_admission_allowed)
                        s_axi_bresp<=2'b10;
                    else initialize_request<=1;
                end else if(write_offset==9'h180) begin
                    if(w_hold_strb!=4'hf||w_hold_data[31:3]!=0||
                       !(w_hold_data[2:0]==3'b001||w_hold_data[2:0]==3'b010||w_hold_data[2:0]==3'b100))
                        s_axi_bresp<=2'b10;
                    else if(w_hold_data[0])begin
                        if(lead_off_busy||lead_off_unknown||lead_off_failed||lead_off_done)s_axi_bresp<=2'b10;
                        else lead_off_start_request<=1;
                    end else if(w_hold_data[1])begin
                        if(!lead_off_busy||lead_off_unknown)s_axi_bresp<=2'b10;
                        else lead_off_cancel_request<=1;
                    end else if(w_hold_data[2])begin
                        if(lead_off_busy||lead_off_unknown)s_axi_bresp<=2'b10;
                        else lead_off_acknowledge_request<=1;
                    end
                end else if(write_offset==9'h170) begin
                    if(w_hold_strb==4'hf && w_hold_data==32'h4c534531) heartbeat_toggle<=~heartbeat_toggle;
                    else s_axi_bresp<=2'b10;
                end else if(write_offset==9'h144) begin
                    if(w_hold_strb==4'hf && w_hold_data==32'h444d4101 && !dma_prepared)
                        dma_prepared<=1'b1;
                    else s_axi_bresp<=2'b10;
                end else if(write_offset==REG_CONTROL && w_hold_strb[0] &&
                            w_hold_data[0] && (!dma_prepared || !arm_admission_allowed ||
                                lead_off_busy || lead_off_unknown)) begin
                    s_axi_bresp<=2'b10; // ARM before RX prepared is forbidden
                end else if(write_offset==REG_FIFO_CONTROL && w_hold_strb[0] &&
                            (w_hold_data[0] || w_hold_data[1])) begin
                    s_axi_bresp<=2'b10; // no snapshot or consume in DMA mode
                end else if(write_offset==REG_CONTROL&&w_hold_strb[0]) begin
                    if(w_hold_data[0]&&arm_request_toggle==arm_ack_toggle)arm_request_toggle<=~arm_request_toggle;
                    if(w_hold_data[1]&&shutdown_request_toggle==shutdown_ack_toggle)shutdown_request_toggle<=~shutdown_request_toggle;
                    if(w_hold_data[2]&&clear_request_toggle==clear_ack_toggle)clear_request_toggle<=~clear_request_toggle;
                    if(w_hold_data[3]&&snapshot_request_toggle==snapshot_ack_toggle)snapshot_request_toggle<=~snapshot_request_toggle;
                end else if(write_offset==REG_FIFO_CONTROL&&w_hold_strb[0]) begin
                    if(w_hold_data[0]&&fifo_snapshot_request_toggle==fifo_snapshot_ack_toggle)fifo_snapshot_request_toggle<=~fifo_snapshot_request_toggle;
                    if(w_hold_data[1]&&consume_request_toggle==consume_ack_toggle)consume_request_toggle<=~consume_request_toggle;
                    if(w_hold_data[2]&&shutdown_request_toggle==shutdown_ack_toggle)shutdown_request_toggle<=~shutdown_request_toggle;
                    if(w_hold_data[3]&&fault_request_toggle==fault_ack_toggle)fault_request_toggle<=~fault_request_toggle;
                    if(w_hold_data[4]&&clear_request_toggle==clear_ack_toggle)clear_request_toggle<=~clear_request_toggle;
                end else if(write_offset!=REG_CONTROL&&write_offset!=REG_FIFO_CONTROL)s_axi_bresp<=2'b10;
            end else if(s_axi_bvalid&&s_axi_bready)s_axi_bvalid<=0;
            if(s_axi_arready&&s_axi_arvalid) begin
                s_axi_rvalid<=1;
                if(is_readable(read_offset))begin s_axi_rdata<=read_mux(read_offset);s_axi_rresp<=0;end
                else begin s_axi_rdata<=0;s_axi_rresp<=2'b10;end
            end else if(s_axi_rvalid&&s_axi_rready)s_axi_rvalid<=0;
        end
    end
endmodule
