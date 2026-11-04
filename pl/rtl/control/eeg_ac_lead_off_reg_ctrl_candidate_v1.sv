// OFFLINE CANDIDATE ONLY: not connected to production AXI/driver, no board authorization.

// Terminal candidate lead-off controller. It shares the acquisition command
// PHY through eeg_lead_off_phy_owner_terminal_v1 and never owns a second SPI.
module eeg_ac_lead_off_reg_ctrl_candidate_v1 #(
    parameter integer CAL_SAMPLES=1024,
    parameter integer CAL_DISCARD=64,
    parameter integer STATUS_SAMPLES=5,
    parameter integer STATUS_TIMEOUT_CYCLES=100_000_000,
    parameter integer SPI_TIMEOUT_CYCLES=100_000,
    parameter integer QUIESCE_TIMEOUT_CYCLES=100_000_000
) (
    input wire clk,input wire rst_n,input wire start,input wire cancel,input wire acknowledge,
    input wire owner_grant,input wire ids_verified,
    input wire acq_quiesced,input wire spi_busy,input wire spi_done,input wire spi_fault,
    input wire [215:0] adc1_rx_bits,input wire [215:0] adc2_rx_bits,
    // Candidate-only controlled AC6nA and one selected positive channel.
    // config bit8 enable, bits5:4 FLEAD_OFF (01 or10), bits3:0 channel.
    // The production controller and its fixed DC workflow remain independent.
    input wire [31:0] calibration_config,
    input wire [215:0] calibration_adc1_frame,input wire [215:0] calibration_adc2_frame,
    input wire [9:0] calibration_read_index,
    output reg signed [31:0] calibration_read_sample,
    output reg [31:0] calibration_generation,
    output reg [31:0] calibration_sample_count,
    output reg [31:0] calibration_bound_config,
    output wire [31:0] calibration_result_flags,
    input wire frame_status_valid,input wire [23:0] adc1_status_word,
    input wire [23:0] adc2_status_word,
    output reg quiesce_request,output reg diagnostic_run,
    output reg spi_start,output reg [1:0] spi_adc_mask,
    output reg [8:0] spi_transfer_bits,output reg [215:0] spi_tx_bits,
    output reg [31:0] spi_interbyte_wait_mask,
    output reg busy,output reg done,output reg failed,output reg unknown_after_failure,
    output reg [7:0] error_code,output reg [15:0] status_samples_adc1,
    output reg [15:0] status_samples_adc2,
    output reg [7:0] positive_off_mask_adc1,output reg [7:0] negative_off_mask_adc1,
    output reg [7:0] positive_off_mask_adc2,output reg [7:0] negative_off_mask_adc2,
    output wire bias_measurement_supported,
    output reg restore_verified_adc1,output reg restore_verified_adc2,
    output reg eeg_continuous_resumed,
    output reg [4:0] phase_status,output reg [4:0] register_index_status,
    output reg error_adc,output reg [7:0] error_register_address,
    output reg [7:0] error_expected,output reg [7:0] error_actual
);
    localparam [4:0] IDLE=0,QUIESCE_ENTRY=1,SDATAC_ENTRY=2,SNAPSHOT=3,
        CONFIG_WRITE=4,CONFIG_READ=5,RDATAC_ENTRY=6,SAMPLE=7,QUIESCE_EXIT=8,
        SDATAC_EXIT=9,DISABLE_WRITE=10,DISABLE_READ=11,RESTORE_WRITE=12,
        RESTORE_READ=13,RDATAC_EXIT=14,DONE=15,UNKNOWN=16,REFUSED=17;
    localparam [7:0] E_OWNER=1,E_ID_GATE=2,E_SRB1=3,E_STATUS_HEADER=4,
        E_STATUS_TIMEOUT=5,E_CONFIG_RREG=6,E_RESTORE_RREG=7,E_CANCEL=8,E_SPI=9,
        E_SPI_TIMEOUT=10;
    localparam integer SNAPSHOT_REG_COUNT=19,LOFF_REG_COUNT=5;
    localparam [7:0] CONFIG4_LOFF_COMP_ENABLE=8'h02;
    reg [4:0] phase=IDLE;
    reg [4:0] adc_index=0;
    reg [4:0] register_index=0;
    reg [4:0] restore_register_index=0;
    reg read_pending=0;
    reg [31:0] status_timeout=0;
    reg [31:0] spi_timeout=0;
    reg [31:0] quiesce_timeout=0;
    reg cleanup_requested=0,cancel_pending=0;
    reg transaction_changed=0;
    reg [7:0] snapshot[0:1][0:SNAPSHOT_REG_COUNT-1];
    integer i;
    (* ram_style="block" *) reg [23:0] calibration_samples[0:CAL_SAMPLES-1];
    reg calibration_saturated=0;
    wire cal_mode=calibration_bound_config[8];
    // TER4 gain apply: config bit9 = request, bits13:12 = gain code (00 x1, 01 x2, 10 x4, 11 x6), every other bit zero.
    // It reuses this controller's quiesce / SDATAC / 19-register snapshot / write+RREG compare / RDATAC transaction; only the
    // CH1SET and CH2SET gain bits of ADC1 differ from the snapshot, and no lead-off source or AC window is involved.
    wire gain_mode=calibration_bound_config[9];
    wire [1:0] gain_code=calibration_bound_config[13:12];
    wire gain_config_valid=(calibration_config & 32'hffffcfff)==32'h00000200;
    wire cal_config_valid=gain_config_valid ||
        ((calibration_config & 32'hfffffec0)==0 &&
        (!calibration_config[8] || calibration_config[5:4]==2'b01 || calibration_config[5:4]==2'b10));
    wire gain_ok=done && !busy && !failed && !unknown_after_failure && restore_verified_adc1 && restore_verified_adc2 && gain_mode;
    wire [23:0] cal_raw=calibration_bound_config[3]?
        calibration_adc2_frame[191-calibration_bound_config[2:0]*24 -:24]:
        calibration_adc1_frame[191-calibration_bound_config[2:0]*24 -:24];
    // Bits 31:16 carry the CH2SET/CH1SET bytes of ADC1 as read back after a gain apply (zero otherwise); bit 5 = gain apply verified.
    assign calibration_result_flags={gain_mode?snapshot[0][4]:8'd0,gain_mode?snapshot[0][3]:8'd0,10'd0,gain_ok,unknown_after_failure,calibration_saturated,
        (calibration_sample_count==CAL_SAMPLES),cal_mode,
        (done && !busy && !failed && !unknown_after_failure && restore_verified_adc1 && restore_verified_adc2 && cal_mode && calibration_sample_count==CAL_SAMPLES)};
    // Readable only after a frozen window/normal restoration; a separate
    // register/driver gate must reject reads while busy or before valid.
    wire calibration_store_enable=rst_n && phase==SAMPLE && !read_pending &&
        !cancel && !cancel_pending && !spi_fault && frame_status_valid &&
        adc1_status_word[23:20]==4'hc && adc2_status_word[23:20]==4'hc &&
        cal_mode && status_samples_adc1>=CAL_DISCARD && calibration_sample_count<CAL_SAMPLES;
    // The RAM itself has no reset. Generation/count/result-valid gates prevent
    // pre-reset contents from becoming an accepted diagnostic window.
    always @(posedge clk)begin
        if(calibration_store_enable)
            calibration_samples[calibration_sample_count[9:0]]<=cal_raw;
        if(calibration_read_index<CAL_SAMPLES)
            calibration_read_sample<={{8{calibration_samples[calibration_read_index][23]}},calibration_samples[calibration_read_index]};
    end

    function automatic [7:0] snapshot_address(input [4:0] n);
        begin case(n)
            0:snapshot_address=8'h01;1:snapshot_address=8'h02;2:snapshot_address=8'h03;
            3:snapshot_address=8'h05;4:snapshot_address=8'h06;5:snapshot_address=8'h07;
            6:snapshot_address=8'h08;7:snapshot_address=8'h09;8:snapshot_address=8'h0a;
            9:snapshot_address=8'h0b;10:snapshot_address=8'h0c;
            11:snapshot_address=8'h0d;12:snapshot_address=8'h0e;
            13:snapshot_address=8'h15;14:snapshot_address=8'h04;
            15:snapshot_address=8'h0f;16:snapshot_address=8'h10;
            17:snapshot_address=8'h11;default:snapshot_address=8'h17;
        endcase end
    endfunction
    function automatic [4:0] restore_index(input [4:0] n);
        begin case(n)
            0:restore_index=14;1:restore_index=15;2:restore_index=16;
            3:restore_index=17;4:restore_index=18;
            default:restore_index=n-5;
        endcase end
    endfunction
    function automatic [7:0] lead_off_address(input [4:0] n);
        begin case(n)
            0:lead_off_address=8'h04;1:lead_off_address=8'h0f;
            2:lead_off_address=8'h10;3:lead_off_address=8'h11;
            default:lead_off_address=8'h17;
        endcase end
    endfunction
    function automatic [7:0] lead_off_value(input [4:0] n,input [4:0] adc);
        begin case(n)
            0:lead_off_value=cal_mode?{6'd0,calibration_bound_config[5:4]}:8'h00; //6nA only; DC legacy or selected AC.
            1:lead_off_value=cal_mode?(adc[0]==calibration_bound_config[3]?(8'h01 << calibration_bound_config[2:0]):8'h00):8'hff;
            2:lead_off_value=adc==0?8'h01:8'h00; // One shared SRB1 N-side source.
            3:lead_off_value=8'h00;
            default:lead_off_value=snapshot[adc][18]|CONFIG4_LOFF_COMP_ENABLE;
        endcase end
    endfunction
    function automatic [7:0] rx_byte(input [4:0] adc);
        rx_byte=adc==0?adc1_rx_bits[7:0]:adc2_rx_bits[7:0];
    endfunction
    function automatic [4:0] operation_adc(input [4:0] index);
        operation_adc=index[0];
    endfunction
    task automatic launch_command(input [4:0] adc,input [8:0] bits,input [23:0] tx);
        begin
            spi_adc_mask<=adc==0?2'b01:2'b10;
            spi_transfer_bits<=bits;
            spi_tx_bits<={{192{1'b0}},tx};
            spi_interbyte_wait_mask<=bits==24?32'h3:32'd0;
            spi_start<=1;
            spi_timeout<=0;
            read_pending<=1;
        end
    endtask
    assign bias_measurement_supported=1'b0;

    always @(posedge clk or negedge rst_n) begin
        if(!rst_n)begin
            phase<=IDLE;adc_index<=0;register_index<=0;restore_register_index<=0;
            calibration_generation<=0;calibration_sample_count<=0;calibration_bound_config<=0;calibration_saturated<=0;
            read_pending<=0;status_timeout<=0;spi_timeout<=0;quiesce_timeout<=0;
            cleanup_requested<=0;transaction_changed<=0;
            cancel_pending<=0;phase_status<=IDLE;register_index_status<=0;
            error_adc<=0;error_register_address<=0;error_expected<=0;error_actual<=0;
            quiesce_request<=0;diagnostic_run<=0;spi_start<=0;spi_adc_mask<=0;
            spi_transfer_bits<=0;spi_tx_bits<=0;spi_interbyte_wait_mask<=0;
            busy<=0;done<=0;failed<=0;unknown_after_failure<=0;error_code<=0;
            status_samples_adc1<=0;status_samples_adc2<=0;
            positive_off_mask_adc1<=0;negative_off_mask_adc1<=0;
            positive_off_mask_adc2<=0;negative_off_mask_adc2<=0;
            restore_verified_adc1<=0;restore_verified_adc2<=0;eeg_continuous_resumed<=0;
            for(i=0;i<SNAPSHOT_REG_COUNT;i=i+1)begin snapshot[0][i]<=0;snapshot[1][i]<=0;end
        end else begin
            spi_start<=0;phase_status<=phase;register_index_status<=register_index;
            if(cancel && phase!=IDLE && phase!=DONE && phase!=REFUSED && phase!=UNKNOWN)
                cancel_pending<=1;
            if(acknowledge && phase==IDLE && !unknown_after_failure)begin
                done<=0;failed<=0;error_code<=0;error_adc<=0;error_register_address<=0;
                error_expected<=0;error_actual<=0;cleanup_requested<=0;cancel_pending<=0;
            end
            if(spi_fault)begin
                error_code<=E_SPI;phase<=UNKNOWN;unknown_after_failure<=1;failed<=1;
                busy<=0;quiesce_request<=1;diagnostic_run<=0;read_pending<=0;spi_timeout<=0;
            end else if(phase==IDLE)begin
                if(start)begin
                    done<=0;failed<=0;error_code<=0;error_adc<=0;error_register_address<=0;
                    error_expected<=0;error_actual<=0;cleanup_requested<=0;cancel_pending<=0;
                    if(!owner_grant)begin error_code<=E_OWNER;failed<=1;phase<=REFUSED;end
                    else if(!ids_verified)begin error_code<=E_ID_GATE;failed<=1;phase<=REFUSED;end
                    else if(!cal_config_valid)begin error_code<=E_CONFIG_RREG;failed<=1;phase<=REFUSED;end
                    else begin
                        busy<=1;quiesce_request<=1;diagnostic_run<=0;status_timeout<=0;quiesce_timeout<=0;
                        calibration_generation<=calibration_generation+1;
                        calibration_bound_config<=calibration_config;
                        calibration_sample_count<=0;calibration_saturated<=0;
                        status_samples_adc1<=0;status_samples_adc2<=0;
                        positive_off_mask_adc1<=0;negative_off_mask_adc1<=0;
                        positive_off_mask_adc2<=0;negative_off_mask_adc2<=0;
                        restore_verified_adc1<=0;restore_verified_adc2<=0;eeg_continuous_resumed<=0;
                        cleanup_requested<=0;transaction_changed<=0;read_pending<=0;phase<=QUIESCE_ENTRY;
                    end
                end
            end else if(phase==REFUSED)begin phase<=IDLE;end
            else if(phase==UNKNOWN)begin quiesce_request<=1;diagnostic_run<=0;busy<=0;failed<=1;unknown_after_failure<=1;end
            else if(phase==QUIESCE_ENTRY)begin
                if(cancel)begin error_code<=E_CANCEL;failed<=1;phase<=RDATAC_EXIT;adc_index<=0;end
                else if(acq_quiesced)begin adc_index<=0;phase<=SDATAC_ENTRY;quiesce_timeout<=0;end
                else if(quiesce_timeout>=QUIESCE_TIMEOUT_CYCLES)begin
                    error_code<=8'h0a;failed<=1;busy<=0;quiesce_request<=0;phase<=IDLE;
                end else quiesce_timeout<=quiesce_timeout+1;
            end else if(phase==QUIESCE_EXIT&&!acq_quiesced)begin
                if(quiesce_timeout>=QUIESCE_TIMEOUT_CYCLES)begin
                    error_code<=8'h0b;failed<=1;unknown_after_failure<=1;busy<=0;
                    quiesce_request<=1;diagnostic_run<=0;phase<=UNKNOWN;
                end else quiesce_timeout<=quiesce_timeout+1;
            end else if(read_pending)begin
                if(spi_done)begin
                    read_pending<=0;spi_timeout<=0;
                    if(cancel_pending || cancel)begin
                        failed<=1;error_code<=E_CANCEL;cleanup_requested<=1;diagnostic_run<=0;
                        quiesce_request<=1;adc_index<=0;register_index<=0;cancel_pending<=0;
                        phase<=transaction_changed?QUIESCE_EXIT:RDATAC_EXIT;
                    end else case(phase)
                        SNAPSHOT:begin
                            snapshot[adc_index][register_index]<=rx_byte(adc_index);
                            if(register_index==SNAPSHOT_REG_COUNT-1)begin
                                if(adc_index==0)begin adc_index<=1;register_index<=0;end
                                else if(!(snapshot[0][13][5]&&snapshot[1][13][5]))begin
                                    error_code<=E_SRB1;failed<=1;error_adc<=1;
                                    error_register_address<=8'h15;error_expected<=8'h20;
                                    error_actual<=snapshot[1][13];phase<=RDATAC_EXIT;adc_index<=0;
                                end else if(gain_mode)begin
                                    snapshot[0][3]<={snapshot[0][3][7],1'b0,gain_code,snapshot[0][3][3:0]};
                                    snapshot[0][4]<={snapshot[0][4][7],1'b0,gain_code,snapshot[0][4][3:0]};
                                    transaction_changed<=1;adc_index<=0;restore_register_index<=0;phase<=RESTORE_WRITE;
                                end else begin adc_index<=0;register_index<=0;phase<=CONFIG_WRITE;end
                            end else register_index<=register_index+1;
                        end
                        CONFIG_WRITE:phase<=CONFIG_READ;
                        CONFIG_READ:begin
                            if(rx_byte(adc_index)!=lead_off_value(register_index,adc_index))begin
                                error_code<=E_CONFIG_RREG;failed<=1;cleanup_requested<=1;
                                error_adc<=adc_index[0];error_register_address<=lead_off_address(register_index);
                                error_expected<=lead_off_value(register_index,adc_index);error_actual<=rx_byte(adc_index);
                                adc_index<=0;register_index<=0;phase<=DISABLE_WRITE;
                            end else if(register_index==LOFF_REG_COUNT-1)begin
                                if(adc_index==0)begin adc_index<=1;register_index<=0;phase<=CONFIG_WRITE;end
                                else begin adc_index<=0;phase<=RDATAC_ENTRY;end
                            end else begin register_index<=register_index+1;phase<=CONFIG_WRITE;end
                        end
                        SDATAC_ENTRY:begin
                            if(adc_index==0)begin adc_index<=1;phase<=SDATAC_ENTRY;end
                            else begin adc_index<=0;register_index<=0;phase<=SNAPSHOT;end
                        end
                        RDATAC_ENTRY:begin
                            if(adc_index==0)begin adc_index<=1;phase<=RDATAC_ENTRY;end
                            else begin
                                adc_index<=0;quiesce_request<=0;diagnostic_run<=1;
                                status_timeout<=0;phase<=SAMPLE;
                            end
                        end
                        SDATAC_EXIT:begin
                            if(adc_index==0)begin adc_index<=1;phase<=SDATAC_EXIT;end
                            else begin adc_index<=0;register_index<=0;phase<=DISABLE_WRITE;end
                        end
                        DISABLE_WRITE:phase<=DISABLE_READ;
                        DISABLE_READ:begin
                            if(rx_byte(adc_index)!=
                                (register_index==0?8'h00:register_index==1?8'h00:
                                 (snapshot[adc_index][18]|CONFIG4_LOFF_COMP_ENABLE)))begin
                                error_code<=E_RESTORE_RREG;phase<=UNKNOWN;unknown_after_failure<=1;
                                failed<=1;busy<=0;quiesce_request<=1;diagnostic_run<=0;
                                error_adc<=adc_index[0];error_register_address<=register_index==0?8'h0f:
                                    register_index==1?8'h10:8'h17;
                                error_expected<=register_index==0?8'h00:register_index==1?8'h00:
                                    (snapshot[adc_index][18]|CONFIG4_LOFF_COMP_ENABLE);
                                error_actual<=rx_byte(adc_index);
                            end else if(register_index==2)begin
                                if(adc_index==0)begin adc_index<=1;register_index<=0;phase<=DISABLE_WRITE;end
                                else begin adc_index<=0;restore_register_index<=0;phase<=RESTORE_WRITE;end
                            end else begin register_index<=register_index+1;phase<=DISABLE_WRITE;end
                        end
                        RESTORE_WRITE:phase<=RESTORE_READ;
                        RESTORE_READ:begin
                            if(rx_byte(adc_index)!=snapshot[adc_index][restore_index(restore_register_index)])begin
                                error_code<=E_RESTORE_RREG;phase<=UNKNOWN;unknown_after_failure<=1;
                                failed<=1;busy<=0;quiesce_request<=1;diagnostic_run<=0;
                                error_adc<=adc_index[0];
                                error_register_address<=snapshot_address(restore_index(restore_register_index));
                                error_expected<=snapshot[adc_index][restore_index(restore_register_index)];
                                error_actual<=rx_byte(adc_index);
                            end else if(restore_register_index==SNAPSHOT_REG_COUNT-1)begin
                                if(adc_index==0)begin
                                    restore_verified_adc1<=1;adc_index<=1;restore_register_index<=0;phase<=RESTORE_WRITE;
                                end else begin
                                    restore_verified_adc2<=1;adc_index<=0;phase<=RDATAC_EXIT;
                                end
                            end else begin restore_register_index<=restore_register_index+1;phase<=RESTORE_WRITE;end
                        end
                        RDATAC_EXIT:begin
                            if(adc_index==0)begin adc_index<=1;phase<=RDATAC_EXIT;end
                            else begin
                                quiesce_request<=0;diagnostic_run<=0;busy<=0;eeg_continuous_resumed<=1;
                                if(cleanup_requested||failed)begin done<=1;phase<=IDLE;end
                                else begin done<=1;phase<=DONE;end
                            end
                        end
                        default:begin error_code<=E_SPI;phase<=UNKNOWN;unknown_after_failure<=1;failed<=1;busy<=0;end
                    endcase
                end else if(spi_timeout>=SPI_TIMEOUT_CYCLES)begin
                    error_code<=E_SPI_TIMEOUT;phase<=UNKNOWN;unknown_after_failure<=1;failed<=1;
                    busy<=0;quiesce_request<=1;diagnostic_run<=0;read_pending<=0;spi_timeout<=0;
                end else spi_timeout<=spi_timeout+1;
            end else if(cancel_pending || (cancel && phase!=IDLE && phase!=DONE && phase!=REFUSED))begin
                failed<=1;error_code<=E_CANCEL;cleanup_requested<=1;diagnostic_run<=0;
                quiesce_request<=1;adc_index<=0;register_index<=0;cancel_pending<=0;
                phase<=transaction_changed?QUIESCE_EXIT:RDATAC_EXIT;
            end else begin
                case(phase)
                    SDATAC_ENTRY:if(!spi_busy)launch_command(adc_index,8,24'h000011);
                    SNAPSHOT:if(!spi_busy)launch_command(adc_index,24,{8'h20|snapshot_address(register_index),16'h0000});
                    CONFIG_WRITE:if(!spi_busy)begin
                        launch_command(adc_index,24,{8'h40|lead_off_address(register_index),8'h00,
                            lead_off_value(register_index,adc_index)});transaction_changed<=1;
                    end
                    CONFIG_READ:if(!spi_busy)launch_command(adc_index,24,{8'h20|lead_off_address(register_index),16'h0000});
                    RDATAC_ENTRY:if(!spi_busy)launch_command(adc_index,8,24'h000010);
                    SAMPLE:begin
                        if(cancel)begin
                            error_code<=E_CANCEL;failed<=1;cleanup_requested<=1;
                            diagnostic_run<=0;quiesce_request<=1;status_timeout<=0;phase<=QUIESCE_EXIT;
                        end else if(frame_status_valid)begin
                            if(adc1_status_word[23:20]!=4'hc||adc2_status_word[23:20]!=4'hc)begin
                                error_code<=E_STATUS_HEADER;failed<=1;cleanup_requested<=1;
                                diagnostic_run<=0;quiesce_request<=1;phase<=QUIESCE_EXIT;status_timeout<=0;
                            end else begin
                                status_samples_adc1<=status_samples_adc1+1;status_samples_adc2<=status_samples_adc2+1;
                                positive_off_mask_adc1<=positive_off_mask_adc1|adc1_status_word[19:12];
                                negative_off_mask_adc1<=negative_off_mask_adc1|adc1_status_word[11:4];
                                positive_off_mask_adc2<=positive_off_mask_adc2|adc2_status_word[19:12];
                                negative_off_mask_adc2<=negative_off_mask_adc2|adc2_status_word[11:4];
                                status_timeout<=0;
                                if(cal_mode && status_samples_adc1>=CAL_DISCARD && calibration_sample_count<CAL_SAMPLES)begin
                                    calibration_sample_count<=calibration_sample_count+1;
                                    if(cal_raw==24'h7fffff||cal_raw==24'h800000)calibration_saturated<=1;
                                end
                                if(status_samples_adc1>=(cal_mode?CAL_DISCARD+CAL_SAMPLES-1:STATUS_SAMPLES-1)&&
                                   status_samples_adc2>=(cal_mode?CAL_DISCARD+CAL_SAMPLES-1:STATUS_SAMPLES-1))begin
                                    diagnostic_run<=0;quiesce_request<=1;status_timeout<=0;phase<=QUIESCE_EXIT;
                                end
                            end
                        end else if(status_timeout>=STATUS_TIMEOUT_CYCLES)begin
                            error_code<=E_STATUS_TIMEOUT;failed<=1;cleanup_requested<=1;
                            diagnostic_run<=0;quiesce_request<=1;phase<=QUIESCE_EXIT;
                        end else status_timeout<=status_timeout+1;
                    end
                    QUIESCE_EXIT:if(acq_quiesced)begin adc_index<=0;phase<=SDATAC_EXIT;end
                    SDATAC_EXIT:if(!spi_busy)launch_command(adc_index,8,24'h000011);
                    DISABLE_WRITE:if(!spi_busy)begin
                        if(register_index==0)launch_command(adc_index,24,{8'h4f,8'h00,8'h00});
                        else if(register_index==1)launch_command(adc_index,24,{8'h50,8'h00,8'h00});
                        else launch_command(adc_index,24,
                            {8'h57,8'h00,snapshot[adc_index][18]|CONFIG4_LOFF_COMP_ENABLE});
                    end
                    DISABLE_READ:if(!spi_busy)begin
                        if(register_index==0)launch_command(adc_index,24,{8'h2f,16'h0000});
                        else if(register_index==1)launch_command(adc_index,24,{8'h30,16'h0000});
                        else launch_command(adc_index,24,{8'h37,16'h0000});
                    end
                    RESTORE_WRITE:if(!spi_busy)launch_command(adc_index,24,
                        {8'h40|snapshot_address(restore_index(restore_register_index)),8'h00,
                         snapshot[adc_index][restore_index(restore_register_index)]});
                    RESTORE_READ:if(!spi_busy)launch_command(adc_index,24,
                        {8'h20|snapshot_address(restore_index(restore_register_index)),16'h0000});
                    RDATAC_EXIT:if(!spi_busy)launch_command(adc_index,8,24'h000010);
                    DONE:begin phase<=IDLE;done<=1;end
                    default:begin end
                endcase
                if(spi_busy &&
                    (phase==SDATAC_ENTRY || phase==SNAPSHOT || phase==CONFIG_WRITE ||
                     phase==CONFIG_READ || phase==RDATAC_ENTRY || phase==SDATAC_EXIT ||
                     phase==DISABLE_WRITE || phase==DISABLE_READ || phase==RESTORE_WRITE ||
                     phase==RESTORE_READ || phase==RDATAC_EXIT)) begin
                    if(spi_timeout>=SPI_TIMEOUT_CYCLES)begin
                        error_code<=E_SPI_TIMEOUT;phase<=UNKNOWN;unknown_after_failure<=1;failed<=1;
                        busy<=0;quiesce_request<=1;diagnostic_run<=0;spi_timeout<=0;
                    end else spi_timeout<=spi_timeout+1;
                end
            end
        end
    end
endmodule
