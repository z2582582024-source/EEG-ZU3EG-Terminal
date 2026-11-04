`timescale 1ns/1ps

// Toggle/acknowledge command CDC plus a coherent acquisition-to-AXI snapshot.
// The snapshot bus is captured in the acquisition domain, held stable, and
// acknowledged only after two acquisition clocks.  The AXI domain samples the
// held bus continuously and may consume it once snapshot_ack_toggle matches the
// request toggle.
module snapshot_cdc_v2 #(
    parameter integer STATUS_WIDTH = 1088
) (
    input  wire                    axi_clk,
    input  wire                    axi_rst_n,
    input  wire                    acq_clk,
    input  wire                    acq_rst_n,
    input  wire                    arm_request_toggle,
    input  wire                    shutdown_request_toggle,
    input  wire                    clear_request_toggle,
    input  wire                    snapshot_request_toggle,
    input  wire [STATUS_WIDTH-1:0] acq_status_live,
    output reg                     acq_arm_pulse,
    output reg                     acq_shutdown_pulse,
    output reg                     acq_clear_pulse,
    output reg                     arm_ack_toggle,
    output reg                     shutdown_ack_toggle,
    output reg                     clear_ack_toggle,
    output reg                     snapshot_ack_toggle,
    output reg [STATUS_WIDTH-1:0]  axi_status_snapshot
);
    (* ASYNC_REG = "TRUE" *) reg [1:0] arm_sync;
    (* ASYNC_REG = "TRUE" *) reg [1:0] shutdown_sync;
    (* ASYNC_REG = "TRUE" *) reg [1:0] clear_sync;
    (* ASYNC_REG = "TRUE" *) reg [1:0] snapshot_sync;
    reg arm_ack_acq;
    reg shutdown_ack_acq;
    reg clear_ack_acq;
    reg snapshot_ack_acq;
    (* ASYNC_REG = "TRUE" *) reg [1:0] arm_ack_sync;
    (* ASYNC_REG = "TRUE" *) reg [1:0] shutdown_ack_sync;
    (* ASYNC_REG = "TRUE" *) reg [1:0] clear_ack_sync;
    (* ASYNC_REG = "TRUE" *) reg [1:0] snapshot_ack_sync;
    reg snapshot_pending;
    reg [1:0] snapshot_delay;
    // Preserve one source launch register per held logical bit.
    // Existing request/hold/delayed-ack protocol is unchanged; scoped XDC bounds data flight.
    (* DONT_TOUCH="TRUE" *) reg [STATUS_WIDTH-1:0] acq_snapshot_hold;
    (* ASYNC_REG = "TRUE" *) reg [STATUS_WIDTH-1:0] snapshot_bus_sync0;
    (* ASYNC_REG = "TRUE" *) reg [STATUS_WIDTH-1:0] snapshot_bus_sync1;

    always @(posedge acq_clk or negedge acq_rst_n) begin
        if (!acq_rst_n) begin
            arm_sync <= 2'b00;
            shutdown_sync <= 2'b00;
            clear_sync <= 2'b00;
            snapshot_sync <= 2'b00;
            acq_arm_pulse <= 1'b0;
            acq_shutdown_pulse <= 1'b0;
            acq_clear_pulse <= 1'b0;
            arm_ack_acq <= 1'b0;
            shutdown_ack_acq <= 1'b0;
            clear_ack_acq <= 1'b0;
            snapshot_ack_acq <= 1'b0;
            snapshot_pending <= 1'b0;
            snapshot_delay <= 2'd0;
            acq_snapshot_hold <= {STATUS_WIDTH{1'b0}};
        end else begin
            arm_sync <= {arm_sync[0], arm_request_toggle};
            shutdown_sync <= {shutdown_sync[0], shutdown_request_toggle};
            clear_sync <= {clear_sync[0], clear_request_toggle};
            snapshot_sync <= {snapshot_sync[0], snapshot_request_toggle};
            acq_arm_pulse <= 1'b0;
            acq_shutdown_pulse <= 1'b0;
            acq_clear_pulse <= 1'b0;

            if (arm_sync[1] != arm_ack_acq) begin
                acq_arm_pulse <= 1'b1;
                arm_ack_acq <= arm_sync[1];
            end
            if (shutdown_sync[1] != shutdown_ack_acq) begin
                acq_shutdown_pulse <= 1'b1;
                shutdown_ack_acq <= shutdown_sync[1];
            end
            if (clear_sync[1] != clear_ack_acq) begin
                acq_clear_pulse <= 1'b1;
                clear_ack_acq <= clear_sync[1];
            end

            if (!snapshot_pending && snapshot_sync[1] != snapshot_ack_acq) begin
                acq_snapshot_hold <= acq_status_live;
                snapshot_pending <= 1'b1;
                snapshot_delay <= 2'd0;
            end else if (snapshot_pending) begin
                if (snapshot_delay == 2'd2) begin
                    snapshot_ack_acq <= snapshot_sync[1];
                    snapshot_pending <= 1'b0;
                end else begin
                    snapshot_delay <= snapshot_delay + 2'd1;
                end
            end
        end
    end

    always @(posedge axi_clk or negedge axi_rst_n) begin
        if (!axi_rst_n) begin
            arm_ack_sync <= 2'b00;
            shutdown_ack_sync <= 2'b00;
            clear_ack_sync <= 2'b00;
            snapshot_ack_sync <= 2'b00;
            arm_ack_toggle <= 1'b0;
            shutdown_ack_toggle <= 1'b0;
            clear_ack_toggle <= 1'b0;
            snapshot_ack_toggle <= 1'b0;
            snapshot_bus_sync0 <= {STATUS_WIDTH{1'b0}};
            snapshot_bus_sync1 <= {STATUS_WIDTH{1'b0}};
            axi_status_snapshot <= {STATUS_WIDTH{1'b0}};
        end else begin
            arm_ack_sync <= {arm_ack_sync[0], arm_ack_acq};
            shutdown_ack_sync <= {shutdown_ack_sync[0], shutdown_ack_acq};
            clear_ack_sync <= {clear_ack_sync[0], clear_ack_acq};
            snapshot_ack_sync <= {snapshot_ack_sync[0], snapshot_ack_acq};
            arm_ack_toggle <= arm_ack_sync[1];
            shutdown_ack_toggle <= shutdown_ack_sync[1];
            clear_ack_toggle <= clear_ack_sync[1];
            snapshot_ack_toggle <= snapshot_ack_sync[1];
            snapshot_bus_sync0 <= acq_snapshot_hold;
            snapshot_bus_sync1 <= snapshot_bus_sync0;
            axi_status_snapshot <= snapshot_bus_sync1;
        end
    end
endmodule
