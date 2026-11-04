#!/usr/bin/env python3
"""Resource / timing history of the PL builds: parse the hierarchical utilization report (top row) and timing_numbers.tsv of every build directory.
usage: summarize_builds.py <builds-root> <out.tsv> <out.md>"""
import sys
from pathlib import Path

BUILDS = [
    ('pl_ter4_build04', 'EDP3 bridge, 244 SPS, first TER4 build'),
    ('pl_ter5_build07', 'TER5: 976.5625 SPS front end + decimation + ECG front'),
    ('pl_ter5_health08', '+ per-channel signal health (first attempt)'),
    ('pl_ter5_health09', '+ signal health (post-synthesis fix)'),
    ('pl_ter5_fft16_10', '+ 16-channel FFT (EEG/ECG spectrum in PL)'),
    ('pl_ter5_ssvep_11', '+ SSVEP eTRCA unit (final)'),
]
DEVICE = {'LUT': 70560, 'FF': 141120, 'BRAM': 216, 'DSP': 360}      # XCZU3EG-SFVC784


def top_row(path):
    header = None
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if cells and cells[0] == 'Instance':
            header = cells
        elif header and cells[0] == 'terminal_board_top':
            return dict(zip(header, cells))
    raise SystemExit('no top row in %s' % path)


def number(row, *names):
    for n in names:
        if n in row and row[n].replace('.', '').isdigit():
            return float(row[n])
    raise KeyError(names)


def main():
    root, out_tsv, out_md = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    rows = []
    for name, note in BUILDS:
        d = root / name
        u = top_row(d / 'utilization.rpt')
        lut = number(u, 'Total LUTs')
        ff = number(u, 'FFs')
        r36 = number(u, 'RAMB36')
        r18 = number(u, 'RAMB18')
        dsp = number(u, 'DSP Blocks')
        tn = dict(l.split('\t') for l in (d / 'timing_numbers.tsv').read_text().split('\n') if l.strip())
        rows.append((name, note, lut, ff, r36 + r18 / 2.0, dsp, float(tn['setup']), float(tn['hold'])))
    with open(out_tsv, 'w', encoding='utf-8', newline='\n') as f:
        f.write('build\tdescription\tLUT\tFF\tBRAM_tiles\tDSP\tWNS_ns\tWHS_ns\n')
        for r in rows:
            f.write('%s\t%s\t%d\t%d\t%.1f\t%d\t%.3f\t%.3f\n' % r)
    with open(out_md, 'w', encoding='utf-8', newline='\n') as f:
        f.write('| build | content | LUT (of %d) | FF | BRAM tiles (of %d) | DSP | WNS ns | WHS ns |\n' % (DEVICE['LUT'], DEVICE['BRAM']))
        f.write('| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |\n')
        for n, note, lut, ff, br, dsp, ws, wh in rows:
            f.write('| %s | %s | %d (%.1f %%) | %d | %.1f (%.1f %%) | %d | %.3f | %.3f |\n'
                    % (n, note, lut, 100 * lut / DEVICE['LUT'], ff, br, 100 * br / DEVICE['BRAM'], dsp, ws, wh))
    print(out_md.read_text(encoding='utf-8'))


if __name__ == '__main__':
    main()
