`timescale 1ns/1ps
// PS EMIO[0]: operator/software supply authorization (NOT voltage sensing).
// PS EMIO[1]: board-level PL reset release. GPIO tri-state always denies.
// Safe at configuration and on every PS fabric reset; no autonomous start.
module board_ps_permissions(
    input wire clk, ps_resetn,
    // Imported PS3.4 exposes EMIO[94:0]; explicitly consume only bits0/1.
    input wire [94:0] gpio_output, gpio_tristate,
    output wire platform_resetn, supply_authorized
);
    wire requested_release=gpio_output[1]&&!gpio_tristate[1];
    wire raw_release=ps_resetn&&requested_release;
    wire raw_supply=raw_release&&gpio_output[0]&&!gpio_tristate[0];
    (* ASYNC_REG="TRUE" *) reg [2:0] release_sync=3'b000;
    (* ASYNC_REG="TRUE" *) reg [2:0] supply_sync=3'b000;
    always @(posedge clk or negedge raw_release)
        if(!raw_release)release_sync<=0;
        else release_sync<={release_sync[1:0],1'b1};
    always @(posedge clk or negedge raw_supply)
        if(!raw_supply)supply_sync<=0;
        else supply_sync<={supply_sync[1:0],1'b1};
    assign platform_resetn=release_sync[2];
    assign supply_authorized=supply_sync[2]&&platform_resetn;
endmodule
