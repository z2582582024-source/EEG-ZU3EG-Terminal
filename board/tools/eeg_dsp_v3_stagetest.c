/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Human-free bench test of the TER4/EDP3 provider, same call sequence as the operator terminal:
 *   INFO -> INITIALIZE -> contact diagnostic -> ACK -> START -> read + validate N EDP3 packets -> normal STOP (drain to COMPLETE) -> RESET.
 * Every packet is validated with eeg_dsp_v3_validate and kept in <out_prefix>_sNN.bin as evidence.
 * It never clears a fault: any fault/unknown state ends the run with the state printed, STOP is attempted only if START was issued.
 * TER5: an optional fourth argument is a comma list of CH01/CH02 gains (1,2,4,6), one per session; the gain is written right after INITIALIZE
 * through EEG_DSP_IOC_ECG_GAIN_SET, polled to READY, and checked against the CH1SET/CH2SET readback; RESET must return it to 6.
 * SSVEP live tap check (environment SSVEP_TAP=1, needs the SSVEP PL + module): right after START the unit's tap is enabled with the capture RAM on, a window
 * is armed 300 decimated frames ahead, and after the packets the captured sub-band samples are read back into <out_prefix>_ssvep.bin
 * (golden/compare_tap_capture.py compares them with the golden decimator + filter bank run on the very same packets).
 * usage: eeg_dsp_v3_stagetest <out_prefix> <sessions> <packets_per_session> [gain,gain,...] */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#include "eeg_dsp_packet_v3.h"
#include "eeg_dsp_uapi_v2.h"
#include "lead_off_terminal.h"

static double now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
static void sleep_ms(int ms) { struct timespec t = {ms / 1000, (ms % 1000) * 1000000L}; nanosleep(&t, NULL); }

static int get_status(int fd, struct eeg_dsp_v2_status *s)
{
    memset(s, 0, sizeof *s);
    if (ioctl(fd, EEG_DSP_IOC_V2_STATUS, s)) return -errno;
    return (s->magic == EEG_DSP_V2_UAPI_MAGIC && s->version == EEG_DSP_V2_UAPI_VERSION) ? 0 : -EPROTO;
}
static void print_status(const char *tag, const struct eeg_dsp_v2_status *s)
{
    printf("STATUS %s state=%u flags=0x%x error=%d packets_completed=%u frames_completed=%u published=%u dropped=%u in_ovf=%u fft_ovf=%u stage=%u epoch=%u\n",
           tag, s->state, s->flags, s->error, s->packets_completed, s->frames_completed, s->packets_published, s->dropped_packets,
           s->input_overflow, s->fft_overflow, s->stage, s->reset_epoch);
    fflush(stdout);
}
/* wait until pred(state) or fault/timeout; returns 0 ok, <0 error */
static int wait_state(int fd, unsigned want, double timeout_s, struct eeg_dsp_v2_status *s)
{
    double end = now_s() + timeout_s;
    for (;;) {
        int rc = get_status(fd, s);
        if (rc) return rc;
        if (s->state == EEG_DSP_V2_STATE_FAULT || s->error) return -EIO;
        if (s->state == want) return 0;
        if (now_s() > end) return -ETIMEDOUT;
        sleep_ms(20);
    }
}

struct chan_stat { int64_t sum; int32_t mn, mx; uint64_t n; };
/* PL signal health of the last packet (EDP3 flag 0x400): bytes of the 120-byte block, decoded in the summary */
static uint8_t g_health[EEG_DSP_V3_HEALTH_BYTES]; static int g_health_seen;

static int g_gain[3];
static int g_ssvep_tap;
static uint32_t g_tap_cur0, g_tap_arm;
static int ssvep_get(int fd, unsigned idx, uint32_t *v) { struct eeg_dsp_ssvep_reg r = {idx, 0}; if (ioctl(fd, EEG_DSP_IOC_SSVEP_REG_GET, &r)) return -errno; *v = r.value; return 0; }
static int ssvep_set(int fd, unsigned idx, uint32_t v) { struct eeg_dsp_ssvep_reg r = {idx, v}; return ioctl(fd, EEG_DSP_IOC_SSVEP_REG_SET, &r) ? -errno : 0; }
/* tap on, capture on (xshift 0), armed 300 decimated frames after the first live frame */
static int ssvep_tap_setup(int fd)
{
    uint32_t id = 0, c0 = 0, c1 = 0;
    int rc;
    double end;
    if ((rc = ssvep_get(fd, EEG_DSP_SSVEP_REG_ID, &id))) return rc;
    if (id != EEG_DSP_SSVEP_ID) return -EPROTO;
    if ((rc = ssvep_set(fd, EEG_DSP_SSVEP_REG_CTRL, (1u << 1) | (1u << 3) | (1u << 5) | (1u << 7) | (1u << 8)))) return rc;   /* abort, clear err/fifo/capture, filter-bank clear */
    if ((rc = ssvep_set(fd, EEG_DSP_SSVEP_REG_CFG, (1u << 11) | (1u << 10)))) return rc;
    if ((rc = ssvep_set(fd, 18, 800))) return rc;                                                                               /* CAP_MAX */
    end = now_s() + 3.0;
    do { sleep_ms(20); if ((rc = ssvep_get(fd, EEG_DSP_SSVEP_REG_CUR_SEQ, &c0))) return rc; } while (c0 == 0 && now_s() < end);
    sleep_ms(300);
    if ((rc = ssvep_get(fd, EEG_DSP_SSVEP_REG_CUR_SEQ, &c1))) return rc;
    printf("SSVEP_TAP cur_seq %u -> %u after 0.3 s (expect +73)\n", c0, c1);
    if (c1 == c0) return -EIO;
    g_tap_cur0 = c1;
    g_tap_arm = c1 + 300;
    if ((rc = ssvep_set(fd, EEG_DSP_SSVEP_REG_ARM_SEQ, g_tap_arm))) return rc;
    return ssvep_set(fd, EEG_DSP_SSVEP_REG_CTRL, 1u);
}
#define SSVEP_CAP_WORDS_PER_FRAME 20u
static int ssvep_tap_readout(int fd, const char *prefix)
{
    static uint32_t words[800 * SSVEP_CAP_WORDS_PER_FRAME];
    struct eeg_dsp_ssvep_bulk b;
    uint32_t status = 0, err = 0, len = 0, cur = 0, hdr[8];
    char path[512];
    FILE *f;
    int rc;
    if ((rc = ssvep_get(fd, EEG_DSP_SSVEP_REG_STATUS, &status)) || (rc = ssvep_get(fd, 30, &err)) || (rc = ssvep_get(fd, EEG_DSP_SSVEP_REG_CAP_LEN, &len)) ||
        (rc = ssvep_get(fd, EEG_DSP_SSVEP_REG_CUR_SEQ, &cur))) return rc;
    len &= 0xffff;
    printf("SSVEP_TAP status=0x%08x err=0x%x capture_frames=%u cur_seq=%u arm_seq=%u\n", status, err, len, cur, g_tap_arm);
    if (len < 400 || len > 800) return -EIO;
    memset(&b, 0, sizeof b);
    b.pointer = 0; b.words = len * SSVEP_CAP_WORDS_PER_FRAME; b.user = (uint64_t)(uintptr_t)words;
    if (ioctl(fd, EEG_DSP_IOC_SSVEP_CAPREAD, &b)) return -errno;
    snprintf(path, sizeof path, "%s_ssvep.bin", prefix);
    if (!(f = fopen(path, "wb"))) return -errno;
    hdr[0] = 0x31565353u; hdr[1] = g_tap_cur0; hdr[2] = g_tap_arm; hdr[3] = len; hdr[4] = status; hdr[5] = err; hdr[6] = cur; hdr[7] = 0;
    fwrite(hdr, 4, 8, f); fwrite(words, 4, b.words, f); fclose(f);
    return 0;
}

static int gain_status(int fd, struct eeg_dsp_ecg_gain_status *g)
{
    memset(g, 0, sizeof *g);
    if (ioctl(fd, EEG_DSP_IOC_ECG_GAIN_STATUS, g)) return -errno;
    return (g->magic == EEG_DSP_ECG_GAIN_STATUS_MAGIC && g->bytes == sizeof *g) ? 0 : -EPROTO;
}
static unsigned ch_set_byte(unsigned gain) { return gain == 6 ? 0x30 : gain == 4 ? 0x20 : gain == 2 ? 0x10 : 0x00; }
static void print_gain(const char *tag, const struct eeg_dsp_ecg_gain_status *g)
{
    printf("GAIN %s requested=%u applied=%u flags=0x%x error=%d ch1set=0x%02x ch2set=0x%02x generation=%u epoch=%u\n", tag, g->requested_gain,
           g->applied_gain, g->flags, g->error, g->readback_ch1set, g->readback_ch2set, g->generation, g->reset_epoch);
    fflush(stdout);
}
/* set CH01/CH02 gain and wait for the verified result; the provider returns to READY with INITIALIZED only */
static int apply_gain(int fd, unsigned gain, struct eeg_dsp_v2_status *st)
{
    struct eeg_dsp_ecg_gain_request rq = {0};
    struct eeg_dsp_ecg_gain_status g;
    double end = now_s() + 20.0, t0 = now_s();
    int rc;
    rq.version = 1; rq.gain = gain;
    if (ioctl(fd, EEG_DSP_IOC_ECG_GAIN_SET, &rq)) return -errno;
    for (;;) {
        if ((rc = get_status(fd, st))) return rc;
        if (st->state == EEG_DSP_V2_STATE_FAULT || st->error) { print_status("gain_fault", st); return -EIO; }
        if ((rc = gain_status(fd, &g))) return rc;
        if (st->state == EEG_DSP_V2_STATE_READY && !(g.flags & EEG_DSP_ECG_GAIN_PENDING)) break;
        if (now_s() > end) { print_gain("timeout", &g); return -ETIMEDOUT; }
        sleep_ms(10);
    }
    print_gain("applied", &g);
    printf("GAIN_APPLY_SECONDS %.3f\n", now_s() - t0);
    if (g.error || !(g.flags & EEG_DSP_ECG_GAIN_VERIFIED) || g.applied_gain != gain || g.readback_ch1set != ch_set_byte(gain) ||
        g.readback_ch2set != ch_set_byte(gain)) return -EPROTO;
    return 0;
}

static int session(int fd, const char *prefix, int index, int want_packets)
{
    static uint8_t pkt[EEG_DSP_V3_PACKET_BYTES];
    struct eeg_dsp_v2_status st;
    struct eeg_dsp_v2_start start = {0};
    struct eeg_dsp_v2_leadoff_request req = {0};
    struct eeg_dsp_v2_leadoff_status lo;
    struct chan_stat cs[16];
    char path[512];
    FILE *keep;
    int rc, started = 0, got = 0, bad = 0, i, c;
    uint32_t expect_first = 0, expect_seq = 0;
    double t0, t_first = 0, t_last = 0;
    uint64_t cyc_span_ok = 0, cyc_total = 0, events = 0, spec_packets = 0;
    uint32_t flag_or = 0;

    for (c = 0; c < 16; ++c) { cs[c].sum = 0; cs[c].mn = INT32_MAX; cs[c].mx = INT32_MIN; cs[c].n = 0; }
    snprintf(path, sizeof path, "%s_s%02d.bin", prefix, index);
    keep = fopen(path, "wb");
    if (!keep) return -errno;

    if ((rc = get_status(fd, &st))) goto out;
    print_status("start_of_session", &st);
    if (st.state != EEG_DSP_V2_STATE_IDLE && st.state != EEG_DSP_V2_STATE_READY) { rc = -EBUSY; goto out; }

    if (st.state == EEG_DSP_V2_STATE_IDLE) {
        printf("STEP initialize\n"); fflush(stdout);
        if (ioctl(fd, EEG_DSP_IOC_V2_INITIALIZE)) { rc = -errno; goto out; }
        if ((rc = wait_state(fd, EEG_DSP_V2_STATE_READY, 60.0, &st))) { print_status("init_failed", &st); goto out; }
        print_status("after_initialize", &st);
        if (!(st.flags & EEG_DSP_V2_STATUS_INITIALIZED)) { rc = -EPROTO; goto out; }
    }

    if (index >= 1 && index <= 3 && g_gain[index - 1]) {
        printf("STEP gain %d\n", g_gain[index - 1]); fflush(stdout);
        {   /* a finished contact result left by an earlier owner must be acknowledged before the gain may change (as the Qt backend does) */
            struct eeg_dsp_v2_leadoff_request ack = {0};
            ack.flags = EEG_DSP_V2_LEADOFF_ACK_RESULT;
            if (st.state == EEG_DSP_V2_STATE_READY && ioctl(fd, EEG_DSP_IOC_V2_LEADOFF_REQUEST, &ack) == 0) printf("ACK of a previous contact result sent\n");
        }
        if ((rc = apply_gain(fd, (unsigned)g_gain[index - 1], &st))) { printf("GAIN_FAIL rc=%d\n", rc); goto out; }
        print_status("after_gain", &st);
        if (!(st.flags & EEG_DSP_V2_STATUS_INITIALIZED)) { rc = -EPROTO; goto out; }
    }
    printf("STEP contact_diagnostic\n"); fflush(stdout);
    {
        uint32_t gen0;
        double end;
        memset(&lo, 0, sizeof lo);
        if (ioctl(fd, EEG_DSP_IOC_V2_LEADOFF_STATUS, &lo)) { rc = -errno; goto out; }
        gen0 = lo.generation;
        req.flags = EEG_DSP_V2_LEADOFF_START;
        if (ioctl(fd, EEG_DSP_IOC_V2_LEADOFF_REQUEST, &req)) { rc = -errno; goto out; }
        end = now_s() + 40.0;
        for (;;) {   /* done = a new generation with DONE set and the provider back in READY */
            if ((rc = get_status(fd, &st))) goto out;
            if (st.state == EEG_DSP_V2_STATE_FAULT || st.error) { rc = -EIO; print_status("diag_failed", &st); goto out; }
            memset(&lo, 0, sizeof lo);
            if (ioctl(fd, EEG_DSP_IOC_V2_LEADOFF_STATUS, &lo)) { rc = -errno; goto out; }
            if (lo.generation > gen0 && (lo.status & LEAD_OFF_STATUS_DONE) && st.state == EEG_DSP_V2_STATE_READY) break;
            if (now_s() > end) { rc = -ETIMEDOUT; print_status("diag_timeout", &st); goto out; }
            sleep_ms(20);
        }
    }
    printf("LEADOFF status=0x%x error=%u sample_counts=0x%x off_masks=0x%x phase=%u generation=%u\n", lo.status, lo.error, lo.sample_counts,
           lo.off_masks, lo.phase, lo.generation);
    if (!(lo.status & LEAD_OFF_STATUS_DONE) || (lo.status & LEAD_OFF_STATUS_UNKNOWN)) { rc = -EPROTO; goto out; }
    req.flags = EEG_DSP_V2_LEADOFF_ACK_RESULT;
    if (ioctl(fd, EEG_DSP_IOC_V2_LEADOFF_REQUEST, &req)) { rc = -errno; goto out; }
    if ((rc = get_status(fd, &st))) goto out;
    print_status("after_ack", &st);

    printf("STEP start\n"); fflush(stdout);
    t0 = now_s();
    if (ioctl(fd, EEG_DSP_IOC_V2_START, &start)) { rc = -errno; goto out; }
    started = 1;
    if (g_ssvep_tap && (rc = ssvep_tap_setup(fd))) { printf("SSVEP_TAP_SETUP_FAIL rc=%d\n", rc); goto out_started; }

    while (got < want_packets) {
        struct pollfd p = {fd, POLLIN, 0};
        ssize_t n;
        int pr = poll(&p, 1, 3000);
        if (pr < 0 && errno == EINTR) continue;
        if (pr <= 0) { rc = pr == 0 ? -ETIMEDOUT : -errno; get_status(fd, &st); print_status("read_timeout", &st); break; }
        if (p.revents & (POLLERR | POLLHUP | POLLNVAL)) { rc = -EIO; break; }
        n = read(fd, pkt, sizeof pkt);
        if (n < 0 && (errno == EAGAIN || errno == EINTR)) continue;
        if (n != (ssize_t)sizeof pkt) { rc = n < 0 ? -errno : -EMSGSIZE; break; }
        t_last = now_s();
        if (!got) t_first = t_last;
        {
            int v = eeg_dsp_v3_validate(pkt, sizeof pkt);
            uint32_t seq = eeg_dsp_v3_le32(pkt + 16), first = eeg_dsp_v3_le32(pkt + 20), flags = eeg_dsp_v3_le32(pkt + 12);
            uint64_t c0 = eeg_dsp_v3_le64(pkt + 112), c1 = eeg_dsp_v3_le64(pkt + 120);
            if (v || seq != expect_seq || first != expect_first) {
                ++bad;
                printf("PACKET_BAD index=%d validate=%d seq=%u(expect %u) first=%u(expect %u) flags=0x%x\n", got, v, seq, expect_seq, first, expect_first, flags);
            }
            expect_seq = seq + 1; expect_first = first + EEG_DSP_V3_RAW_FRAMES;
            flag_or |= flags;
            if (flags & EEG_DSP_V3_FLAG_HEALTH) { memcpy(g_health, pkt + EEG_DSP_V3_HEALTH_OFFSET, sizeof g_health); ++g_health_seen; }
            if (flags & EEG_DSP_V3_FLAG_SPECTRUM_VALID) ++spec_packets;
            events += eeg_dsp_v3_le32(pkt + EEG_DSP_V3_STATUS_OFFSET + 40);
            cyc_total += 1;
            if (c1 > c0 && (c1 - c0) == (uint64_t)(EEG_DSP_V3_RAW_FRAMES - 1) * 102400ULL) ++cyc_span_ok;
            for (i = 0; i < (int)EEG_DSP_V3_RAW_FRAMES; ++i)
                for (c = 0; c < 16; ++c) {
                    int32_t x = (int32_t)eeg_dsp_v3_le32(pkt + EEG_DSP_V3_RAW_OFFSET + i * EEG_DSP_V3_RAW_FRAME_BYTES + 24 + c * 4);
                    cs[c].sum += x; cs[c].n++; if (x < cs[c].mn) cs[c].mn = x; if (x > cs[c].mx) cs[c].mx = x;
                }
            fwrite(pkt, 1, sizeof pkt, keep);
        }
        ++got;
        if (got <= 2 || got % 10 == 0) {
            get_status(fd, &st); print_status("running", &st);
        }
    }

out_started:
    if (started && g_ssvep_tap && !rc) { int r3 = ssvep_tap_readout(fd, prefix); if (r3) { printf("SSVEP_TAP_READOUT_FAIL rc=%d\n", r3); rc = r3; } else printf("SSVEP_TAP_READOUT_OK\n"); }
    if (started) {
        double end;
        printf("STEP stop (after %d packets)\n", got); fflush(stdout);
        if (ioctl(fd, EEG_DSP_IOC_V2_STOP)) printf("STOP ioctl errno=%d\n", errno);
        end = now_s() + 20.0;
        for (;;) {   /* drain until the provider reports COMPLETE */
            struct pollfd p = {fd, POLLIN, 0};
            ssize_t n;
            int r2 = get_status(fd, &st);
            if (r2) { rc = rc ? rc : r2; break; }
            if (st.state == EEG_DSP_V2_STATE_COMPLETE) break;
            if (st.state == EEG_DSP_V2_STATE_FAULT || st.error) { rc = rc ? rc : -EIO; break; }
            if (now_s() > end) { rc = rc ? rc : -ETIMEDOUT; break; }
            if (poll(&p, 1, 200) > 0 && (p.revents & POLLIN)) {
                n = read(fd, pkt, sizeof pkt);
                if (n == (ssize_t)sizeof pkt) {
                    int v = eeg_dsp_v3_validate(pkt, sizeof pkt);
                    uint32_t seq = eeg_dsp_v3_le32(pkt + 16), first = eeg_dsp_v3_le32(pkt + 20);
                    if (v || seq != expect_seq || first != expect_first) { ++bad; printf("DRAIN_PACKET_BAD validate=%d seq=%u first=%u\n", v, seq, first); }
                    expect_seq = seq + 1; expect_first = first + EEG_DSP_V3_RAW_FRAMES;
                    fwrite(pkt, 1, sizeof pkt, keep); ++got;
                }
            }
        }
        print_status("after_stop", &st);
        /* leftover packets after COMPLETE must be zero before RESET is allowed */
        for (;;) {
            struct pollfd p = {fd, POLLIN, 0};
            if (poll(&p, 1, 100) <= 0 || !(p.revents & POLLIN)) break;
            if (read(fd, pkt, sizeof pkt) == (ssize_t)sizeof pkt) {
                int v = eeg_dsp_v3_validate(pkt, sizeof pkt);
                uint32_t seq = eeg_dsp_v3_le32(pkt + 16), first = eeg_dsp_v3_le32(pkt + 20);
                if (v || seq != expect_seq || first != expect_first) ++bad;
                expect_seq = seq + 1; expect_first = first + EEG_DSP_V3_RAW_FRAMES;
                fwrite(pkt, 1, sizeof pkt, keep); ++got;
            }
        }
    }

    if (!rc) {
        const unsigned need = EEG_DSP_V2_STATUS_NORMAL_STOP_VERIFIED | EEG_DSP_V2_STATUS_RESET_HELD;
        double dt = got > 1 ? t_last - t_first : 0;
        printf("SUMMARY session=%d packets=%d bad=%d spectrum_packets=%llu candidate_events=%llu cycle_span_exact=%llu/%llu flags_or=0x%x rate_packets_per_s=%.3f (expect 7.629)\n",
               index, got, bad, (unsigned long long)spec_packets, (unsigned long long)events, (unsigned long long)cyc_span_ok,
               (unsigned long long)cyc_total, flag_or, dt > 0 ? (got - 1) / dt : 0.0);
        for (c = 0; c < 16; ++c)
            printf("CH%02d n=%llu mean=%.1f min=%d max=%d\n", c + 1, (unsigned long long)cs[c].n, cs[c].n ? (double)cs[c].sum / cs[c].n : 0.0, cs[c].mn, cs[c].mx);
        if (g_health_seen) {
            printf("HEALTH_PACKETS %d (last packet, PL-computed over 128 frames)\n", g_health_seen);
            for (c = 0; c < 16; ++c) {
                const uint8_t *r = g_health + 4 + c * EEG_DSP_V3_HEALTH_CHANNEL_BYTES;
                int32_t mean = (int32_t)(r[0] | (r[1] << 8) | (r[2] << 16)); if (mean & 0x800000) mean |= ~0xffffff;
                printf("HEALTH CH%02d mean=%d rms_code=%u hum_code=%u rail=%u flags=0x%02x\n", c + 1, mean, r[3], r[4], r[5], r[6]);
            }
        }
        (void)t0;
        if (get_status(fd, &st) || st.state != EEG_DSP_V2_STATE_COMPLETE || (st.flags & need) != need || bad) rc = rc ? rc : -EPROTO;
        print_status("before_reset", &st);
        if (!rc) {
            printf("STEP reset\n"); fflush(stdout);
            if (ioctl(fd, EEG_DSP_IOC_V2_RESET)) { rc = -errno; }
            else if ((rc = wait_state(fd, EEG_DSP_V2_STATE_IDLE, 20.0, &st))) print_status("reset_failed", &st);
            else print_status("after_reset", &st);
            if (!rc && g_gain[index - 1]) {
                struct eeg_dsp_ecg_gain_status g;
                if (gain_status(fd, &g)) rc = -EPROTO;
                else { print_gain("after_reset", &g); if (g.applied_gain != 6) rc = -EPROTO; }
            }
        }
    } else {
        get_status(fd, &st); print_status("error_state", &st);
    }
out:
    fclose(keep);
    return rc;
}

int main(int argc, char **argv)
{
    struct eeg_dsp_v2_info info;
    int fd, rc = 0, s, sessions, packets;
    if (argc != 4 && argc != 5) { fprintf(stderr, "usage: %s out_prefix sessions packets [gain,gain,...]\n", argv[0]); return 2; }
    sessions = atoi(argv[2]); packets = atoi(argv[3]);
    g_ssvep_tap = getenv("SSVEP_TAP") && getenv("SSVEP_TAP")[0] == '1';
    if (argc == 5) {
        char *p = argv[4];
        for (s = 0; s < 3 && *p; ++s) {
            g_gain[s] = (int)strtol(p, &p, 10);
            if (g_gain[s] != 1 && g_gain[s] != 2 && g_gain[s] != 4 && g_gain[s] != 6) return 2;
            if (*p == ',') ++p;
        }
    }
    if (sessions < 1 || sessions > 3 || packets < 2 || packets > 400) return 2;
    fd = open(EEG_DSP_V2_DEVICE_PATH, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) { fprintf(stderr, "open failed: %s\n", strerror(errno)); return 1; }
    memset(&info, 0, sizeof info);
    if (ioctl(fd, EEG_DSP_IOC_V2_INFO, &info) || info.magic != EEG_DSP_V2_UAPI_MAGIC || info.packet_bytes != EEG_DSP_V3_PACKET_BYTES ||
        info.sample_interval_ns != EEG_DSP_V3_RAW_SAMPLE_INTERVAL_NS || info.algorithm_id != EEG_DSP_V3_ALGORITHM_ID ||
        !(info.flags & EEG_DSP_V2_INFO_EDP3_STREAM) || (info.flags & EEG_DSP_V2_INFO_EDP2_STREAM)) {
        fprintf(stderr, "INFO is not the TER4/EDP3 contract (packet_bytes=%u interval=%u algo=0x%x flags=0x%x)\n", info.packet_bytes,
                info.sample_interval_ns, info.algorithm_id, info.flags);
        close(fd);
        return 3;
    }
    if (argc == 5 && !(info.flags & EEG_DSP_V2_INFO_ECG_GAIN)) { fprintf(stderr, "provider has no ECG gain support (flags=0x%x)\n", info.flags); close(fd); return 3; }
    printf("INFO_OK packet_bytes=%u interval_ns=%u algorithm=0x%x flags=0x%x\n", info.packet_bytes, info.sample_interval_ns, info.algorithm_id, info.flags);
    for (s = 1; s <= sessions && !rc; ++s) {
        printf("=== SESSION %d ===\n", s); fflush(stdout);
        rc = session(fd, argv[1], s, packets);
        printf("SESSION %d RESULT %s (%d)\n", s, rc ? "FAIL" : "PASS", rc);
    }
    close(fd);
    printf("STAGETEST_%s\n", rc ? "FAIL" : "PASS");
    return rc ? 1 : 0;
}
