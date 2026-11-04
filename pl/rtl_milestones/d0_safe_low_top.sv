`timescale 1ns/1ps

// Milestone 4B safe-low top.
//
// This design is intentionally not an ADS1299 controller.  The seven
// PL-to-ADS signals are connected to explicit OBUFs whose inputs are tied to
// constant zero.  The four ADS-to-PL signals are observation-only inputs.
// There is no SPI engine, VIO, ID read, PWDN release, or RESET release.
module d0_safe_low_top (
    input  wire sys_clk_p,
    input  wire sys_clk_n,

    input  wire adc1_dout,
    input  wire adc2_dout,
    input  wire adc1_drdy_n,
    input  wire adc2_drdy_n,

    output wire adc_din,
    output wire adc_pwdn_n,
    output wire adc_reset_n,
    output wire adc_sclk,
    output wire adc_start,
    output wire adc1_cs_n,
    output wire adc2_cs_n
);

    wire pl_clk;

    // Bit order: DIN, PWDN_N, RESET_N, SCLK, START, ADC1_CS_N, ADC2_CS_N.
    (* KEEP = "TRUE" *) wire [6:0] safe_outputs = 7'b0000000;

    (* ASYNC_REG = "TRUE" *) reg [1:0] adc1_dout_sync = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] adc2_dout_sync = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] adc1_drdy_sync = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] adc2_drdy_sync = 2'b00;

    reg [31:0] safe_counter = 32'd0;

    wire [3:0] raw_inputs = {
        adc2_dout,
        adc1_dout,
        adc2_drdy_n,
        adc1_drdy_n
    };
    wire [3:0] synchronized_inputs = {
        adc2_dout_sync[1],
        adc1_dout_sync[1],
        adc2_drdy_sync[1],
        adc1_drdy_sync[1]
    };
    wire [3:0] safe_state = 4'd0;
    wire [7:0] status_flags = {
        7'd0,
        (safe_outputs == 7'd0)
    };

    IBUFDS #(
        .DIFF_TERM("FALSE"),
        .IBUF_LOW_PWR("TRUE")
    ) sys_clk_ibufds (
        .I (sys_clk_p),
        .IB(sys_clk_n),
        .O (pl_clk)
    );

    // Explicit OBUFs make the post-synthesis and post-route constant-low
    // connection auditable.  Do not replace these inputs with registers.
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_din (
        .I(safe_outputs[6]),
        .O(adc_din)
    );
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_pwdn_n (
        .I(safe_outputs[5]),
        .O(adc_pwdn_n)
    );
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_reset_n (
        .I(safe_outputs[4]),
        .O(adc_reset_n)
    );
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_sclk (
        .I(safe_outputs[3]),
        .O(adc_sclk)
    );
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_start (
        .I(safe_outputs[2]),
        .O(adc_start)
    );
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc1_cs_n (
        .I(safe_outputs[1]),
        .O(adc1_cs_n)
    );
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc2_cs_n (
        .I(safe_outputs[0]),
        .O(adc2_cs_n)
    );

    always @(posedge pl_clk) begin
        safe_counter   <= safe_counter + 32'd1;
        adc1_dout_sync <= {adc1_dout_sync[0], adc1_dout};
        adc2_dout_sync <= {adc2_dout_sync[0], adc2_dout};
        adc1_drdy_sync <= {adc1_drdy_sync[0], adc1_drdy_n};
        adc2_drdy_sync <= {adc2_drdy_sync[0], adc2_drdy_n};
    end

    // Observation only.  No VIO is instantiated and no probe can drive an
    // ADS-facing output.
    ila_d0_safe ila_i (
        .clk   (pl_clk),
        .probe0(safe_outputs),
        .probe1(synchronized_inputs),
        .probe2(safe_state),
        .probe3(safe_counter),
        .probe4(status_flags),
        .probe5(raw_inputs)
    );

endmodule
