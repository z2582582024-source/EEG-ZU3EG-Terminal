`timescale 1ns/1ps

// AXI-Lite leaf for the lead-off block. Control writes are one-hot W1P with a
// full strobe; UNKNOWN is read-only and has no software clear operation.
module eeg_lead_off_axi_regs_terminal_v1 #(
    parameter integer ADDR_WIDTH=9
)(
    input wire aclk,input wire aresetn,
    input wire [ADDR_WIDTH-1:0] awaddr,input wire awvalid,output wire awready,
    input wire [31:0] wdata,input wire [3:0] wstrb,input wire wvalid,output wire wready,
    output reg [1:0] bresp,output reg bvalid,input wire bready,
    input wire [ADDR_WIDTH-1:0] araddr,input wire arvalid,output wire arready,
    output reg [31:0] rdata,output reg [1:0] rresp,output reg rvalid,input wire rready,
    output reg start_request,output reg cancel_request,output reg acknowledge_result,
    input wire busy,input wire done,input wire failed,input wire unknown_latched,
    input wire diagnostic_owner,input wire acquisition_quiesced,input wire diagnostic_run,
    input wire restore_adc1_verified,input wire restore_adc2_verified,input wire continuous_resumed,
    input wire id_gate_passed,
    input wire [7:0] error_code,input wire [15:0] samples_adc1,input wire [15:0] samples_adc2,
    input wire [7:0] positive_off_adc1,input wire [7:0] negative_off_adc1,
    input wire [7:0] positive_off_adc2,input wire [7:0] negative_off_adc2,
    input wire [31:0] owner_state,input wire [4:0] phase,input wire [4:0] register_index,
    input wire error_adc,input wire [7:0] error_register_address,
    input wire [7:0] error_expected,input wire [7:0] error_actual
);
    reg aw_pending,w_pending;
    reg [ADDR_WIDTH-1:0] aw_hold;
    reg [31:0] w_hold;
    reg [3:0] strb_hold;
    wire [8:0] write_offset={aw_hold[8:2],2'b00};
    wire [8:0] read_offset={araddr[8:2],2'b00};
    wire write_fire=aw_pending&&w_pending&&!bvalid;
    wire one_hot_control=(w_hold[31:3]==0)&&
        ((w_hold[2:0]==3'b001)||(w_hold[2:0]==3'b010)||(w_hold[2:0]==3'b100));
    assign awready=!aw_pending&&!bvalid;
    assign wready=!w_pending&&!bvalid;
    assign arready=!rvalid;
    function automatic readable(input [8:0] a);
        begin case(a)
            9'h184,9'h188,9'h18c,9'h190,9'h194,9'h198,9'h19c:readable=1'b1;
            default:readable=1'b0;
        endcase end
    endfunction
    function automatic [31:0] read_mux(input [8:0] a);
        begin case(a)
            9'h184:read_mux={21'd0,id_gate_passed,continuous_resumed,restore_adc2_verified,
                restore_adc1_verified,diagnostic_run,acquisition_quiesced,diagnostic_owner,
                unknown_latched,failed,done,busy};
            9'h188:read_mux={24'd0,error_code};
            9'h18c:read_mux={samples_adc2,samples_adc1};
            9'h190:read_mux={negative_off_adc2,positive_off_adc2,negative_off_adc1,positive_off_adc1};
            9'h194:read_mux=owner_state;
            9'h198:read_mux={6'd0,(error_code!=0),error_adc,error_register_address,
                error_expected,error_actual};
            9'h19c:read_mux={22'd0,register_index,phase};
            default:read_mux=0;
        endcase end
    endfunction
    always @(posedge aclk or negedge aresetn)begin
        if(!aresetn)begin
            aw_pending<=0;w_pending<=0;aw_hold<=0;w_hold<=0;strb_hold<=0;
            bresp<=0;bvalid<=0;rdata<=0;rresp<=0;rvalid<=0;
            start_request<=0;cancel_request<=0;acknowledge_result<=0;
        end else begin
            start_request<=0;cancel_request<=0;acknowledge_result<=0;
            if(awready&&awvalid)begin aw_pending<=1;aw_hold<=awaddr;end
            if(wready&&wvalid)begin w_pending<=1;w_hold<=wdata;strb_hold<=wstrb;end
            if(write_fire)begin
                aw_pending<=0;w_pending<=0;bvalid<=1;bresp<=2'b00;
                if(write_offset!=9'h180||strb_hold!=4'hf||!one_hot_control)
                    bresp<=2'b10;
                else if(w_hold[0])begin
                    if(busy||unknown_latched||failed||done)bresp<=2'b10;
                    else start_request<=1;
                end else if(w_hold[1])begin
                    if(!busy||unknown_latched)bresp<=2'b10;
                    else cancel_request<=1;
                end else if(w_hold[2])begin
                    if(busy||unknown_latched)bresp<=2'b10;
                    else acknowledge_result<=1;
                end
            end else if(bvalid&&bready)bvalid<=0;
            if(arready&&arvalid)begin
                rvalid<=1;
                if(readable(read_offset))begin rdata<=read_mux(read_offset);rresp<=0;end
                else begin rdata<=0;rresp<=2'b10;end
            end else if(rvalid&&rready)rvalid<=0;
        end
    end
endmodule
