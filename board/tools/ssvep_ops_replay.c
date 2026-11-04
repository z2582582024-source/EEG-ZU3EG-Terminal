/* Board replay of the SSVEP unit bench program.
 *
 * golden/gen_unit_vectors.py writes ops.hex: 4 x 32-bit words per bus operation {op, byte offset, data, mask} for the RTL bench
 * (tb_ssvep_unit.sv).  This tool runs the very same program against the real PL through the driver ioctls (register window 0x200..0x27F),
 * so a PASS means the hardware reproduces the golden model bit for bit: parameter CRC, window alignment, all checkpoint scores, decision
 * records (CRC-checked), result spaces, capture RAM, latency counters.   op 1 write | 2 read and compare ((rd ^ data) & mask == 0) |
 * 3 wait until STATUS busy == 0 | 4 wait until the record FIFO holds >= data records | 5 check the CRC of the last 16 record words read |
 * 6 delay `data` PL clocks | 7 end.
 *
 * build on the board:  gcc -O2 -I<rtl_dsp> ssvep_ops_replay.c -o ssvep_ops_replay
 * usage: ssvep_ops_replay ops.hex [/dev/eeg-dsp] [--stats]     exit 0 = PASS
 */
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#include "eeg_dsp_uapi_v2.h"

static int fd = -1;

static int rd(uint32_t byte_offset, uint32_t *value)
{
    if (byte_offset < EEG_DSP_SSVEP_WINDOW_BASE || byte_offset >= EEG_DSP_SSVEP_WINDOW_BASE + 4U * EEG_DSP_SSVEP_WINDOW_WORDS) {
        errno = EINVAL;                      /* outside 0x200..0x27F the PL answers SLVERR = CPU external abort */
        return -1;
    }
    struct eeg_dsp_ssvep_reg r = {(byte_offset - EEG_DSP_SSVEP_WINDOW_BASE) / 4U, 0};
    if (ioctl(fd, EEG_DSP_IOC_SSVEP_REG_GET, &r) < 0) return -1;
    *value = r.value;
    return 0;
}

static int wr(uint32_t byte_offset, uint32_t value)
{
    if (byte_offset < EEG_DSP_SSVEP_WINDOW_BASE || byte_offset >= EEG_DSP_SSVEP_WINDOW_BASE + 4U * EEG_DSP_SSVEP_WINDOW_WORDS ||
        !(EEG_DSP_SSVEP_WRITABLE_MASK & (1U << ((byte_offset - EEG_DSP_SSVEP_WINDOW_BASE) / 4U)))) {
        errno = EINVAL;                      /* a write to a read-only word is answered with SLVERR */
        return -1;
    }
    struct eeg_dsp_ssvep_reg r = {(byte_offset - EEG_DSP_SSVEP_WINDOW_BASE) / 4U, value};
    return ioctl(fd, EEG_DSP_IOC_SSVEP_REG_SET, &r);
}

static uint32_t crc_state(const uint32_t *w, int n)
{
    uint32_t c = 0xFFFFFFFFu;
    for (int i = 0; i < n; ++i) {
        c ^= w[i];
        for (int k = 0; k < 32; ++k) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
    }
    return c;
}

static double now_s(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + 1e-9 * (double)t.tv_nsec;
}

static int read_space(uint32_t pointer, uint32_t *out, uint32_t words)
{
    struct eeg_dsp_ssvep_bulk b = {pointer, words, (uint64_t)(uintptr_t)out, 0, 0};
    return ioctl(fd, EEG_DSP_IOC_SSVEP_PREAD, &b);
}

/* latency percentile from the 64-bin histogram (bin width 4096 PL clocks = 40.96 us) */
static void print_latency(const char *name, uint32_t pointer)
{
    uint32_t h[64];
    if (read_space(pointer, h, 64) < 0) {
        printf("LATENCY %s read failed: %s\n", name, strerror(errno));
        return;
    }
    uint64_t total = 0;
    for (int i = 0; i < 64; ++i) total += h[i];
    if (!total) {
        printf("LATENCY %s: no samples\n", name);
        return;
    }
    uint64_t acc = 0;
    int p50 = -1, p99 = -1, last = 0;
    for (int i = 0; i < 64; ++i) {
        acc += h[i];
        if (h[i]) last = i;
        if (p50 < 0 && acc * 2 >= total) p50 = i;
        if (p99 < 0 && acc * 100 >= total * 99) p99 = i;
    }
    printf("LATENCY %s: n=%llu  P50<=%.1f us  P99<=%.1f us  max-bin<=%.1f us  (bin 40.96 us)\n", name, (unsigned long long)total,
           (p50 + 1) * 40.96, (p99 + 1) * 40.96, (last + 1) * 40.96);
}

int main(int argc, char **argv)
{
    const char *path = argc > 1 ? argv[1] : NULL;
    const char *dev = EEG_DSP_V2_DEVICE_PATH;
    int stats = 0;
    for (int i = 2; i < argc; ++i) {
        if (!strcmp(argv[i], "--stats")) stats = 1;
        else dev = argv[i];
    }
    if (!path) {
        fprintf(stderr, "usage: %s ops.hex [device] [--stats]\n", argv[0]);
        return 2;
    }
    FILE *f = fopen(path, "r");
    if (!f) {
        perror(path);
        return 2;
    }
    size_t cap = 1 << 20, n = 0;
    uint32_t *ops = malloc(cap * sizeof(uint32_t));
    char line[64];
    while (fgets(line, sizeof line, f)) {
        if (line[0] == '/' || line[0] == '\n' || line[0] == '#') continue;
        if (n == cap) {
            cap *= 2;
            ops = realloc(ops, cap * sizeof(uint32_t));
        }
        ops[n++] = (uint32_t)strtoul(line, NULL, 16);
    }
    fclose(f);
    fd = open(dev, O_RDWR);
    if (fd < 0) {
        perror(dev);
        return 2;
    }
    {   /* a PL without the unit answers this window with an AXI error (external abort): only touch it when the provider says it is there */
        struct eeg_dsp_v2_info info;
        memset(&info, 0, sizeof info);
        if (ioctl(fd, EEG_DSP_IOC_V2_INFO, &info) < 0 || !(info.flags & EEG_DSP_V2_INFO_SSVEP)) {
            fprintf(stderr, "provider does not advertise the SSVEP unit (INFO flags %08x): wrong module/PL pair\n", info.flags);
            return 2;
        }
    }
    uint32_t id = 0;
    if (rd(0x200, &id) < 0) {
        fprintf(stderr, "SSVEP window read failed (%s): wrong pair?\n", strerror(errno));
        return 2;
    }
    if (id != EEG_DSP_SSVEP_ID) {
        fprintf(stderr, "SSVEP_ID_MISMATCH read %08x expected %08x\n", id, EEG_DSP_SSVEP_ID);
        return 1;
    }
    double t0 = now_s();
    uint32_t last16[16];
    unsigned lastn = 0, errors = 0, reads = 0, writes = 0;
    size_t pc = 0;
    for (; pc + 3 < n && ops[pc] != 7; pc += 4) {
        const uint32_t op = ops[pc], addr = ops[pc + 1], data = ops[pc + 2], mask = ops[pc + 3];
        uint32_t rv = 0;
        switch (op) {
        case 1:
            if (wr(addr, data) < 0) {
                if (++errors < 20) printf("WRITE_ERR op@%zu addr %03x: %s\n", pc / 4, addr, strerror(errno));
            }
            ++writes;
            break;
        case 2:
            ++reads;
            if (rd(addr, &rv) < 0) {
                if (++errors < 20) printf("READ_ERR op@%zu addr %03x: %s\n", pc / 4, addr, strerror(errno));
                break;
            }
            if (addr == 0x228) last16[lastn++ % 16] = rv;
            if (((rv ^ data) & mask) != 0 && ++errors < 40)
                printf("MISMATCH op@%zu addr %03x got %08x exp %08x mask %08x\n", pc / 4, addr, rv, data, mask);
            break;
        case 3: {
            double limit = now_s() + 30.0;
            for (;;) {
                if (rd(0x208, &rv) < 0) {
                    ++errors;
                    break;
                }
                if (!(rv & 1u) && !(rv >> 31)) break;
                if (now_s() > limit) {
                    printf("WAIT_IDLE_TIMEOUT op@%zu status %08x\n", pc / 4, rv);
                    ++errors;
                    break;
                }
                usleep(200);
            }
            break;
        }
        case 4: {
            double limit = now_s() + 30.0;
            for (;;) {
                if (rd(0x22c, &rv) < 0) {
                    ++errors;
                    break;
                }
                if ((rv & 0x1f) >= (data & 0x1f)) break;
                if (now_s() > limit) {
                    printf("WAIT_REC_TIMEOUT op@%zu count %u want %u\n", pc / 4, rv & 0x1f, data & 0x1f);
                    ++errors;
                    break;
                }
                usleep(200);
            }
            break;
        }
        case 5: {
            uint32_t w[16];
            for (int k = 0; k < 16; ++k) w[k] = last16[(lastn - 16 + (unsigned)k) % 16];
            if (lastn < 16 || ~crc_state(w, 15) != w[15]) {
                ++errors;
                printf("RECORD_CRC_FAIL op@%zu got %08x exp %08x\n", pc / 4, w[15], ~crc_state(w, 15));
            }
            break;
        }
        case 6:
            usleep(data / 100U + 1U);
            break;
        default:
            printf("BAD_OP %u @%zu\n", op, pc / 4);
            ++errors;
        }
    }
    if (pc + 3 >= n || ops[pc] != 7) {
        printf("OPS_NOT_TERMINATED pc=%zu\n", pc / 4);
        ++errors;
    }
    if (!reads) {
        printf("NO_READS_CHECKED\n");
        ++errors;
    }
    printf("SSVEP_BOARD_REPLAY ops=%zu writes=%u reads=%u errors=%u elapsed=%.2fs\n", pc / 4, writes, reads, errors, now_s() - t0);
    if (stats) {
        uint32_t v[5];
        for (int k = 0; k < 5; ++k) rd(0x200 + 4U * (19U + (unsigned)k), &v[k]);
        printf("CHECKPOINT_LATENCY min=%u max=%u sum=%llu count=%u cycles (10 ns)\n", v[0], v[1], (unsigned long long)v[2] | ((unsigned long long)v[3] << 32), v[4]);
        for (int k = 0; k < 5; ++k) rd(0x200 + 4U * (24U + (unsigned)k), &v[k]);
        printf("FRAME_LATENCY      min=%u max=%u sum=%llu count=%u cycles (10 ns)\n", v[0], v[1], (unsigned long long)v[2] | ((unsigned long long)v[3] << 32), v[4]);
        print_latency("checkpoint", 0x08000);
        print_latency("frame", 0x09000);
    }
    puts(errors ? "SSVEP_BOARD_REPLAY_FAIL" : "SSVEP_BOARD_REPLAY_PASS");
    return errors ? 1 : 0;
}
