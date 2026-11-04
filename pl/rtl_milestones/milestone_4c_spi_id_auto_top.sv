`timescale 1ns/1ps

// Milestone 4C-SPI-ID-AUTO hardware top.  The ILA is observation-only.
module milestone_4c_spi_id_auto_top (
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
    reg [3:0] reset_pipe = 4'b0000;
    wire ctrl_rst_n = reset_pipe[3];
    wire [3:0] raw_inputs = {
        adc2_dout, adc1_dout, adc2_drdy_n, adc1_drdy_n
    };
    wire [4:0]  m4c_state;
    wire [31:0] m4c_counter;
    wire [7:0]  m4c_error_flags;
    wire        m4c_done;
    wire        m4c_shutdown_safe;
    wire        m4c_fault_safe;
    wire        m4c_key_synchronized;
    wire        m4c_key_armed;
    wire        m4c_key_press_event;
    wire [1:0]  m4c_accepted_event_count;
    wire [3:0]  m4c_sync_inputs;
    wire [6:0]  m4c_control_outputs;
    wire        m4c_spi_busy;
    wire        m4c_spi_done;
    wire        m4c_spi_start;
    wire [2:0]  m4c_spi_operation;
    wire [8:0]  m4c_spi_transfer_bits;
    wire [7:0]  m4c_ads1_id_raw;
    wire [7:0]  m4c_ads2_id_raw;
    wire        m4c_ads1_id_valid;
    wire        m4c_ads2_id_valid;
    wire        adc_din_i;
    wire        adc_pwdn_n_i;
    wire        adc_reset_n_i;
    wire        adc_sclk_i;
    wire        adc_start_i;
    wire        adc1_cs_n_i;
    wire        adc2_cs_n_i;

    IBUFDS #(
        .DIFF_TERM("FALSE"),
        .IBUF_LOW_PWR("TRUE")
    ) sys_clk_ibufds (
        .I(sys_clk_p), .IB(sys_clk_n), .O(pl_clk)
    );

    // Deterministic post-configuration reset for both the sequencer and PHY.
    // The external interface remains fail-low until four PL clock edges have
    // initialized all state and SPI registers.
    always @(posedge pl_clk) begin
        reset_pipe <= {reset_pipe[2:0], 1'b1};
    end

    (* DONT_TOUCH = "TRUE" *) milestone_4c_spi_id_auto_ctrl m4c_ctrl (
        .clk(pl_clk),
        .rst_n(ctrl_rst_n),
        .key_n(pl_key_n),
        .raw_inputs(raw_inputs),
        .state(m4c_state),
        .counter(m4c_counter),
        .error_flags(m4c_error_flags),
        .done(m4c_done),
        .shutdown_safe(m4c_shutdown_safe),
        .fault_safe(m4c_fault_safe),
        .key_synchronized(m4c_key_synchronized),
        .key_armed(m4c_key_armed),
        .key_press_event(m4c_key_press_event),
        .accepted_event_count(m4c_accepted_event_count),
        .synchronized_inputs(m4c_sync_inputs),
        .control_outputs(m4c_control_outputs),
        .adc_din(adc_din_i),
        .adc_pwdn_n(adc_pwdn_n_i),
        .adc_reset_n(adc_reset_n_i),
        .adc_sclk(adc_sclk_i),
        .adc_start(adc_start_i),
        .adc1_cs_n(adc1_cs_n_i),
        .adc2_cs_n(adc2_cs_n_i),
        .spi_busy(m4c_spi_busy),
        .spi_done(m4c_spi_done),
        .spi_start(m4c_spi_start),
        .spi_operation(m4c_spi_operation),
        .spi_transfer_bits(m4c_spi_transfer_bits),
        .ads1_id_raw(m4c_ads1_id_raw),
        .ads2_id_raw(m4c_ads2_id_raw),
        .ads1_id_valid(m4c_ads1_id_valid),
        .ads2_id_valid(m4c_ads2_id_valid)
    );

    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_din     (.I(adc_din_i),     .O(adc_din));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_pwdn_n  (.I(adc_pwdn_n_i),  .O(adc_pwdn_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_reset_n (.I(adc_reset_n_i), .O(adc_reset_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_sclk    (.I(adc_sclk_i),    .O(adc_sclk));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc_start   (.I(adc_start_i),   .O(adc_start));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc1_cs_n   (.I(adc1_cs_n_i),   .O(adc1_cs_n));
    (* DONT_TOUCH = "TRUE" *) OBUF obuf_adc2_cs_n   (.I(adc2_cs_n_i),   .O(adc2_cs_n));

    // ILA probe map.  These explicit, numbered aliases are part of the
    // evidence interface: keep probe number, width, and bit order stable.
    // probe00 [4:0]  state implementation encoding
    // probe01 [31:0] state/transaction counter, unsigned
    // probe02 [6:0]  {DIN,PWDN_N,RESET_N,SCLK,START,ADC1_CS_N,ADC2_CS_N}
    // probe03 [1:0]  {PWDN_N,RESET_N}
    // probe04 [7:0]  error flags
    // probe05        done in RESULT_HOLD
    // probe06        shutdown_safe
    // probe07        fault_safe
    // probe08 [3:0]  synchronized ADS inputs
    // probe09 [3:0]  raw ADS inputs
    // probe10        raw active-low PL_KEY0 input
    // probe11 [2:0]  {key_synchronized,key_armed,key_press_event}
    // probe12 [1:0]  accepted_event_count
    // probe13 [2:0]  SPI operation
    // probe14 [2:0]  {spi_busy,spi_done,spi_start}
    // probe15 [1:0]  {ADC1_CS_N,ADC2_CS_N}
    // probe16 [7:0]  ADS1 raw ID
    // probe17 [7:0]  ADS2 raw ID
    // probe18 [1:0]  {ADS1_ID_valid,ADS2_ID_valid}
    // probe19 [8:0]  SPI transfer bit count
    (* KEEP = "TRUE" *) wire [4:0]  ila_probe00_state = m4c_state;
    (* KEEP = "TRUE" *) wire [31:0] ila_probe01_counter = m4c_counter;
    (* KEEP = "TRUE" *) wire [6:0]  ila_probe02_control_outputs = m4c_control_outputs;
    (* KEEP = "TRUE" *) wire [1:0]  ila_probe03_pwdn_reset = {
        adc_pwdn_n_i, adc_reset_n_i
    };
    (* KEEP = "TRUE" *) wire [7:0]  ila_probe04_error_flags = m4c_error_flags;
    (* KEEP = "TRUE" *) wire        ila_probe05_done_result_hold = m4c_done;
    (* KEEP = "TRUE" *) wire        ila_probe06_shutdown_safe = m4c_shutdown_safe;
    (* KEEP = "TRUE" *) wire        ila_probe07_fault_safe = m4c_fault_safe;
    (* KEEP = "TRUE" *) wire [3:0]  ila_probe08_synchronized_inputs = m4c_sync_inputs;
    (* KEEP = "TRUE" *) wire [3:0]  ila_probe09_raw_inputs = raw_inputs;
    (* KEEP = "TRUE" *) wire        ila_probe10_pl_key_n = pl_key_n;
    (* KEEP = "TRUE" *) wire [2:0]  ila_probe11_key_status = {
        m4c_key_synchronized, m4c_key_armed, m4c_key_press_event
    };
    (* KEEP = "TRUE" *) wire [1:0]  ila_probe12_accepted_event_count =
        m4c_accepted_event_count;
    (* KEEP = "TRUE" *) wire [2:0]  ila_probe13_spi_operation = m4c_spi_operation;
    (* KEEP = "TRUE" *) wire [2:0]  ila_probe14_spi_handshake = {
        m4c_spi_busy, m4c_spi_done, m4c_spi_start
    };
    (* KEEP = "TRUE" *) wire [1:0]  ila_probe15_chip_selects = {
        adc1_cs_n_i, adc2_cs_n_i
    };
    (* KEEP = "TRUE" *) wire [7:0]  ila_probe16_ads1_id_raw = m4c_ads1_id_raw;
    (* KEEP = "TRUE" *) wire [7:0]  ila_probe17_ads2_id_raw = m4c_ads2_id_raw;
    (* KEEP = "TRUE" *) wire [1:0]  ila_probe18_id_valid = {
        m4c_ads1_id_valid, m4c_ads2_id_valid
    };
    (* KEEP = "TRUE" *) wire [8:0]  ila_probe19_spi_transfer_bits =
        m4c_spi_transfer_bits;

    ila_m4c ila_i (
        .clk(pl_clk),
        .probe0(ila_probe00_state),
        .probe1(ila_probe01_counter),
        .probe2(ila_probe02_control_outputs),
        .probe3(ila_probe03_pwdn_reset),
        .probe4(ila_probe04_error_flags),
        .probe5(ila_probe05_done_result_hold),
        .probe6(ila_probe06_shutdown_safe),
        .probe7(ila_probe07_fault_safe),
        .probe8(ila_probe08_synchronized_inputs),
        .probe9(ila_probe09_raw_inputs),
        .probe10(ila_probe10_pl_key_n),
        .probe11(ila_probe11_key_status),
        .probe12(ila_probe12_accepted_event_count),
        .probe13(ila_probe13_spi_operation),
        .probe14(ila_probe14_spi_handshake),
        .probe15(ila_probe15_chip_selects),
        .probe16(ila_probe16_ads1_id_raw),
        .probe17(ila_probe17_ads2_id_raw),
        .probe18(ila_probe18_id_valid),
        .probe19(ila_probe19_spi_transfer_bits)
    );

endmodule
