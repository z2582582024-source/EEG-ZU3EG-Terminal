`timescale 1ns/1ps

// Offline 4F bounded paired-frame buffer.  The module stores one complete
// ADS1/ADS2 pair per entry; a single-device event is never exposed as valid.
// All counters are saturating and the sequence protocol is explicit.
module milestone_4f_frame_pair_fifo #(
    parameter integer DEPTH = 4,
    parameter integer SEQ_WIDTH = 32,
    parameter integer ALMOST_FULL_LEVEL = DEPTH-1
) (
    input  wire                         clk,
    input  wire                         rst_n,
    input  wire                         id_gate_passed,
    input  wire                         frame1_valid,
    input  wire                         frame2_valid,
    input  wire [SEQ_WIDTH-1:0]         producer_sequence,
    input  wire [215:0]                 ads1_frame_in,
    input  wire [215:0]                 ads2_frame_in,
    input  wire [23:0]                  ads1_status_in,
    input  wire [23:0]                  ads2_status_in,
    input  wire                         consume_req,
    input  wire                         shutdown_req,
    input  wire                         fault_req,
    output reg                          consume_valid,
    output reg  [SEQ_WIDTH-1:0]         consume_sequence,
    output reg  [215:0]                 consume_ads1_frame,
    output reg  [215:0]                 consume_ads2_frame,
    output reg  [23:0]                  consume_ads1_status,
    output reg  [23:0]                  consume_ads2_status,
    output wire                         empty,
    output wire                         full,
    output wire                         almost_full,
    output wire [31:0]                  occupancy,
    output reg  [31:0]                  overflow_count,
    output reg  [31:0]                  dropped_count,
    output reg  [31:0]                  partial_pair_count,
    output reg  [31:0]                  sequence_gap_count,
    output reg  [SEQ_WIDTH-1:0]         last_accepted_sequence,
    output reg  [SEQ_WIDTH-1:0]         last_consumed_sequence,
    output reg                          sequence_gap,
    output reg                          partial_pair,
    output reg                          overflow,
    output reg                          stopped,
    output reg                          shutdown_safe,
    output reg                          fault_safe,
    output wire                         head_valid,
    output wire [SEQ_WIDTH-1:0]         head_sequence,
    output wire [215:0]                 head_ads1_frame,
    output wire [215:0]                 head_ads2_frame,
    output wire [23:0]                  head_ads1_status,
    output wire [23:0]                  head_ads2_status
);
    localparam integer PTR_WIDTH = (DEPTH <= 2) ? 1 : $clog2(DEPTH);
    reg [SEQ_WIDTH-1:0] seq_mem [0:DEPTH-1];
    reg [215:0] frame1_mem [0:DEPTH-1];
    reg [215:0] frame2_mem [0:DEPTH-1];
    reg [23:0] status1_mem [0:DEPTH-1];
    reg [23:0] status2_mem [0:DEPTH-1];
    reg [PTR_WIDTH-1:0] wr_ptr, rd_ptr;
    reg [PTR_WIDTH:0] count;
    reg have_sequence;
    reg [SEQ_WIDTH-1:0] expected_sequence;
    integer i;

    assign occupancy = count;
    assign empty = (count == 0);
    assign full = (count >= DEPTH);
    assign almost_full = (count >= ALMOST_FULL_LEVEL);
    assign head_valid = !empty;
    assign head_sequence = empty ? {SEQ_WIDTH{1'b0}} : seq_mem[rd_ptr];
    assign head_ads1_frame = empty ? 216'd0 : frame1_mem[rd_ptr];
    assign head_ads2_frame = empty ? 216'd0 : frame2_mem[rd_ptr];
    assign head_ads1_status = empty ? 24'd0 : status1_mem[rd_ptr];
    assign head_ads2_status = empty ? 24'd0 : status2_mem[rd_ptr];

    function automatic [PTR_WIDTH-1:0] next_ptr(input [PTR_WIDTH-1:0] ptr);
        begin
            if (ptr == DEPTH-1) next_ptr = {PTR_WIDTH{1'b0}};
            else next_ptr = ptr + 1'b1;
        end
    endfunction

    function automatic [31:0] sat_inc(input [31:0] value);
        begin
            sat_inc = (value == 32'hFFFF_FFFF) ? value : value + 1'b1;
        end
    endfunction

    wire pair_event = frame1_valid && frame2_valid;
    wire partial_event = frame1_valid ^ frame2_valid;
    // A stop/fault request takes effect for admission in the same cycle.  A
    // pending buffered entry may still be consumed during orderly shutdown,
    // but a fault request blocks both production and consumption immediately.
    wire write_accept = pair_event && id_gate_passed && !stopped &&
                        !shutdown_req && !fault_req && !shutdown_safe &&
                        !fault_safe && !full;
    wire read_accept = consume_req && !empty && !shutdown_safe &&
                       !fault_safe && !fault_req;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wr_ptr <= 0;
            rd_ptr <= 0;
            count <= 0;
            have_sequence <= 1'b0;
            expected_sequence <= 0;
            last_accepted_sequence <= 0;
            last_consumed_sequence <= 0;
            overflow_count <= 0;
            dropped_count <= 0;
            partial_pair_count <= 0;
            sequence_gap_count <= 0;
            sequence_gap <= 1'b0;
            partial_pair <= 1'b0;
            overflow <= 1'b0;
            stopped <= 1'b0;
            shutdown_safe <= 1'b0;
            fault_safe <= 1'b0;
            consume_valid <= 1'b0;
            consume_sequence <= 0;
            consume_ads1_frame <= 0;
            consume_ads2_frame <= 0;
            consume_ads1_status <= 0;
            consume_ads2_status <= 0;
        end else begin
            consume_valid <= 1'b0;
            sequence_gap <= 1'b0;
            partial_pair <= 1'b0;

            if (shutdown_req && !shutdown_safe && !fault_safe) stopped <= 1'b1;
            if (fault_req && !shutdown_safe && !fault_safe) begin
                stopped <= 1'b1;
                fault_safe <= 1'b1;
                shutdown_safe <= 1'b0;
            end

            if (partial_event && !shutdown_safe && !fault_safe) begin
                partial_pair <= 1'b1;
                partial_pair_count <= sat_inc(partial_pair_count);
                dropped_count <= sat_inc(dropped_count);
            end

            if (pair_event && !shutdown_safe && !fault_safe) begin
                if (!id_gate_passed || stopped || shutdown_req || fault_req || fault_safe) begin
                    dropped_count <= sat_inc(dropped_count);
                end else if (full) begin
                    overflow <= 1'b1;
                    overflow_count <= sat_inc(overflow_count);
                    dropped_count <= sat_inc(dropped_count);
                end else begin
                    if (have_sequence && producer_sequence != expected_sequence) begin
                        sequence_gap <= 1'b1;
                        sequence_gap_count <= sat_inc(sequence_gap_count);
                    end
                    seq_mem[wr_ptr] <= producer_sequence;
                    frame1_mem[wr_ptr] <= ads1_frame_in;
                    frame2_mem[wr_ptr] <= ads2_frame_in;
                    status1_mem[wr_ptr] <= ads1_status_in;
                    status2_mem[wr_ptr] <= ads2_status_in;
                    wr_ptr <= next_ptr(wr_ptr);
                    last_accepted_sequence <= producer_sequence;
                    expected_sequence <= producer_sequence + 1'b1;
                    have_sequence <= 1'b1;
                end
            end

            if (read_accept) begin
                consume_valid <= 1'b1;
                consume_sequence <= seq_mem[rd_ptr];
                consume_ads1_frame <= frame1_mem[rd_ptr];
                consume_ads2_frame <= frame2_mem[rd_ptr];
                consume_ads1_status <= status1_mem[rd_ptr];
                consume_ads2_status <= status2_mem[rd_ptr];
                last_consumed_sequence <= seq_mem[rd_ptr];
                rd_ptr <= next_ptr(rd_ptr);
            end

            // Resolve simultaneous producer/consumer activity exactly once.
            // The prior implementation issued two nonblocking assignments to
            // count; the later read assignment won and incorrectly reduced
            // occupancy during a valid simultaneous read/write cycle.
            case ({write_accept, read_accept})
                2'b10: count <= count + 1'b1;
                2'b01: count <= count - 1'b1;
                default: count <= count;
            endcase

            // A shutdown drains naturally; once the final buffered pair is
            // consumed, no new writes are accepted and the state is locked.
            if (!fault_req && stopped && (count == 0 || read_accept))
                shutdown_safe <= !fault_safe;
        end
    end
endmodule
