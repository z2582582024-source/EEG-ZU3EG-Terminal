`timescale 1ns/1ps

// D0-only top level. The source list intentionally excludes every SPI PHY,
// transaction controller, VIO core, register command path, and ID reader.
module d0_timing_top (
    input  wire sys_clk_p,
    input  wire sys_clk_n,
    input  wire pl_key_n,
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
    wire [3:0] raw_inputs = {
        adc2_dout, adc1_dout, adc2_drdy_n, adc1_drdy_n
    };
    wire [2:0] d0_state;
    wire [31:0] d0_counter;
    wire [7:0] d0_error_flags;
    wire d0_done;
    wire d0_arm_start_synchronized;
    wire d0_arm_ready;
    wire d0_arm_start_seen;
    wire [3:0] d0_sync_inputs;
    wire [6:0] d0_control_outputs;
    wire adc_din_i;
    wire adc_pwdn_n_i;
    wire adc_reset_n_i;
    wire adc_sclk_i;
    wire adc_start_i;
    wire adc1_cs_n_i;
    wire adc2_cs_n_i;

    IBUFDS #(
        .DIFF_TERM("FALSE"),
        .IBUF_LOW_PWR("TRUE")
    ) sys_clk_ibufds (
        .I (sys_clk_p),
        .IB(sys_clk_n),
        .O (pl_clk)
    );

    (* DONT_TOUCH = "TRUE" *) d0_timing_ctrl d0_ctrl (
        .clk(pl_clk),
        .arm_start_n(pl_key_n),
        .raw_inputs(raw_inputs),
        .state(d0_state),
        .counter(d0_counter),
        .error_flags(d0_error_flags),
        .done(d0_done),
        .arm_start_synchronized(d0_arm_start_synchronized),
        .arm_ready(d0_arm_ready),
        .arm_start_seen(d0_arm_start_seen),
        .synchronized_inputs(d0_sync_inputs),
        .control_outputs(d0_control_outputs),
        .adc_din(adc_din_i),
        .adc_pwdn_n(adc_pwdn_n_i),
        .adc_reset_n(adc_reset_n_i),
        .adc_sclk(adc_sclk_i),
        .adc_start(adc_start_i),
        .adc1_cs_n(adc1_cs_n_i),
        .adc2_cs_n(adc2_cs_n_i)
    );

    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_din (.I(adc_din_i), .O(adc_din));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_pwdn_n (.I(adc_pwdn_n_i), .O(adc_pwdn_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_reset_n (.I(adc_reset_n_i), .O(adc_reset_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_sclk (.I(adc_sclk_i), .O(adc_sclk));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_start (.I(adc_start_i), .O(adc_start));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc1_cs_n (.I(adc1_cs_n_i), .O(adc1_cs_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc2_cs_n (.I(adc2_cs_n_i), .O(adc2_cs_n));

    // Observation only; there is deliberately no VIO instance.
    ila_d0_timing ila_i (
        .clk(pl_clk),
        .probe0(d0_state),
        .probe1(d0_counter),
        .probe2(d0_control_outputs),
        .probe3({adc_pwdn_n_i, adc_reset_n_i}),
        .probe4(d0_error_flags),
        .probe5(d0_done),
        .probe6(d0_sync_inputs),
        .probe7(raw_inputs),
        .probe8(pl_key_n),
        .probe9({d0_arm_start_synchronized, d0_arm_ready, d0_arm_start_seen})
    );

endmodule
