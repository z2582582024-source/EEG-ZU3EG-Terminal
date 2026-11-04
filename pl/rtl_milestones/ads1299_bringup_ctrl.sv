`timescale 1ns/1ps

// Simple sequential bring-up coordinator.  SPI command/register encoding is
// intentionally delegated to a transaction executor so this state machine
// can later sit above either the local PHY or an AXI-controlled SPI engine.
module ads1299_bringup_ctrl #(
    parameter integer T_POR_SYS_CYCLES = 12_800_000,
    parameter integer T_RESET_LOW_SYS_CYCLES = 100,
    parameter integer T_RESET_RELEASE_SYS_CYCLES = 879,
    parameter integer REFERENCE_WAIT_SYS_CYCLES = 20_000_000
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        begin_bringup,
    output reg         adc_pwdn_n,
    output reg         adc_reset_n,
    output reg         adc_start,
    output reg         op_valid,
    output reg [3:0]   op_code,
    output reg [1:0]   op_adc_mask,
    input  wire        op_done,
    input  wire        op_ok,
    output reg [3:0]   phase,
    output reg         complete,
    output reg         failed
);

    localparam [3:0] PH_IDLE          = 4'd0;
    localparam [3:0] PH_TPOR          = 4'd1;
    localparam [3:0] PH_RESET_LOW     = 4'd2;
    localparam [3:0] PH_RESET_WAIT    = 4'd3;
    localparam [3:0] PH_SDATAC        = 4'd4;
    localparam [3:0] PH_READ_ID       = 4'd5;
    localparam [3:0] PH_WRITE_CONFIG  = 4'd6;
    localparam [3:0] PH_WAIT_REFERENCE= 4'd7;
    localparam [3:0] PH_VERIFY_CONFIG = 4'd8;
    localparam [3:0] PH_RDATAC        = 4'd9;
    localparam [3:0] PH_START         = 4'd10;
    localparam [3:0] PH_COMPLETE      = 4'd11;
    localparam [3:0] PH_FAILED        = 4'd12;

    localparam [3:0] OP_SDATAC        = 4'd1;
    localparam [3:0] OP_READ_ID       = 4'd2;
    localparam [3:0] OP_WRITE_TEST_CFG= 4'd3;
    localparam [3:0] OP_VERIFY_CFG    = 4'd4;
    localparam [3:0] OP_RDATAC        = 4'd5;

    integer wait_counter;
    reg op_issued;

    task enter_failed;
        begin
            adc_start <= 1'b0;
            op_valid <= 1'b0;
            failed <= 1'b1;
            phase <= PH_FAILED;
        end
    endtask

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            adc_pwdn_n <= 1'b0;
            adc_reset_n <= 1'b0;
            adc_start <= 1'b0;
            op_valid <= 1'b0;
            op_code <= 4'd0;
            op_adc_mask <= 2'b00;
            phase <= PH_IDLE;
            complete <= 1'b0;
            failed <= 1'b0;
            wait_counter <= 0;
            op_issued <= 1'b0;
        end else begin
            case (phase)
                PH_IDLE: begin
                    adc_pwdn_n <= 1'b0;
                    adc_reset_n <= 1'b0;
                    adc_start <= 1'b0;
                    op_valid <= 1'b0;
                    complete <= 1'b0;
                    failed <= 1'b0;
                    op_issued <= 1'b0;
                    if (begin_bringup) begin
                        adc_pwdn_n <= 1'b1;
                        adc_reset_n <= 1'b1;
                        wait_counter <= 0;
                        phase <= PH_TPOR;
                    end
                end

                PH_TPOR: begin
                    if (wait_counter >= T_POR_SYS_CYCLES - 1) begin
                        adc_reset_n <= 1'b0;
                        wait_counter <= 0;
                        phase <= PH_RESET_LOW;
                    end else wait_counter <= wait_counter + 1;
                end

                PH_RESET_LOW: begin
                    if (wait_counter >= T_RESET_LOW_SYS_CYCLES - 1) begin
                        adc_reset_n <= 1'b1;
                        wait_counter <= 0;
                        phase <= PH_RESET_WAIT;
                    end else wait_counter <= wait_counter + 1;
                end

                PH_RESET_WAIT: begin
                    if (wait_counter >= T_RESET_RELEASE_SYS_CYCLES - 1) begin
                        wait_counter <= 0;
                        phase <= PH_SDATAC;
                    end else wait_counter <= wait_counter + 1;
                end

                PH_SDATAC, PH_READ_ID, PH_WRITE_CONFIG,
                PH_VERIFY_CONFIG, PH_RDATAC: begin
                    if (!op_issued) begin
                        op_valid <= 1'b1;
                        op_adc_mask <= 2'b11;
                        case (phase)
                            PH_SDATAC: op_code <= OP_SDATAC;
                            PH_READ_ID: op_code <= OP_READ_ID;
                            PH_WRITE_CONFIG: op_code <= OP_WRITE_TEST_CFG;
                            PH_VERIFY_CONFIG: op_code <= OP_VERIFY_CFG;
                            default: op_code <= OP_RDATAC;
                        endcase
                        op_issued <= 1'b1;
                    end else if (op_done) begin
                        op_valid <= 1'b0;
                        op_issued <= 1'b0;
                        if (!op_ok) begin
                            enter_failed();
                        end else begin
                            case (phase)
                                PH_SDATAC: phase <= PH_READ_ID;
                                PH_READ_ID: phase <= PH_WRITE_CONFIG;
                                PH_WRITE_CONFIG: begin
                                    wait_counter <= 0;
                                    phase <= PH_WAIT_REFERENCE;
                                end
                                PH_VERIFY_CONFIG: phase <= PH_RDATAC;
                                default: phase <= PH_START;
                            endcase
                        end
                    end
                end

                PH_WAIT_REFERENCE: begin
                    if (wait_counter >= REFERENCE_WAIT_SYS_CYCLES - 1) begin
                        wait_counter <= 0;
                        phase <= PH_VERIFY_CONFIG;
                    end else wait_counter <= wait_counter + 1;
                end

                PH_START: begin
                    adc_start <= 1'b1;
                    complete <= 1'b1;
                    phase <= PH_COMPLETE;
                end

                PH_COMPLETE: complete <= 1'b1;
                PH_FAILED: begin
                    adc_start <= 1'b0;
                    failed <= 1'b1;
                end
                default: enter_failed();
            endcase
        end
    end

endmodule

