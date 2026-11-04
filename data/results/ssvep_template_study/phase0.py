#!/usr/bin/env python3
"""Task-1 phase 0: does a time-domain (or blended) template beat the deployed Fourier-template eTRCA under the DEPLOYED protocol?

Protocol (what the board runs, see etrca_calib.py / etrca_unit_golden.py / etrca_solver.cpp):
  * 28 targets  = round(i * 40 / 28)  of the Tsinghua benchmark (the self-test pack uses the same subset), 8 posterior channels, product filter bank
  * the scored window starts 122 frames (0.4997 s) after the stimulus onset (ONSET_FRAMES = WINDOW_OFFSET = 122 in make_selftest_pack.py)
  * 8 models / checkpoints of 60 90 122 183 244 305 366 488 frames; model m is calibrated on windows of exactly its own length
  * thresholds from resubstitution on the training windows: THR_S = 0.35 * median(S_best of correctly scored), THR_M = 0.35 * median(margin)
  * a checkpoint is gated when S_best >= THR_S and margin >= THR_M; the decision is taken when 2 consecutive gated checkpoints agree on the target;
    no decision at the last checkpoint = NO_DECISION.  (Quality vetoes are not modelled: clean public data.)
  * 3 calibration rounds, tested on the other 3 blocks; four train/test splits of the 6 blocks.

Schemes (all use the same M = sum_t W_t W_t^T per band and length; they differ only in the template T):
  fourier   deployed: least-squares fit on 5 harmonics, one fit per length
  td_len    time-domain mean of the calibration windows of that length (one template per length)
  td_pref   the 488-frame time-domain mean, cut to the first L frames (one 1.09 MB template for every length)
  blend_len 0.5 fourier + 0.5 time domain, per length
  blend_pref 0.5 * (fourier_488 + td_488) cut to L frames
Scoring: r = <x, M T> / sqrt(<M, x x^T> <M, T T^T>),  S = sum_b W_b sign(r) r^2   (float; the PL's integer version is checked separately).

usage: python phase0.py --json out.json [--workers N] [--subjects 1-10] [--offset 122]
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parents[3] / "src" / "tools" / "ssvep"
sys.path.insert(0, str(TOOLS))
import offline_fbcca as F  # noqa: E402
import pl_trca_model as P  # noqa: E402
import trca_core as T  # noqa: E402
import tsinghua_benchmark as B  # noqa: E402

FS = F.SAMPLE_RATE
LENGTHS = [60, 90, 122, 183, 244, 305, 366, 488]
TARGETS = [int(round(i * 40 / 28)) for i in range(28)]
NT = len(TARGETS)
SPLITS = [([0, 1, 2], [3, 4, 5]), ([3, 4, 5], [0, 1, 2]), ([0, 2, 4], [1, 3, 5]), ([1, 3, 5], [0, 2, 4])]
SCHEMES = ["fourier", "td_len", "td_pref", "blend_len", "blend_pref"]
COMBOS = ["fourier|resub", "fourier|loro", "td_len|loro", "td_pref|loro", "blend_len|loro", "blend_pref|loro", "blend_pref|resub"]
ALPHA = 0.5
F_S, F_M, AGREE = 0.35, 0.35, 2
W = np.asarray(P.WEIGHTS, dtype=float)


def band_models(filt, start, freqs, train, L):
    """-> M (5,8,8), fourier template (5,NT,8,L), time-domain template (5,NT,8,L) for windows of L frames."""
    M = np.empty((5, 8, 8))
    Fo = np.empty((5, NT, 8, L))
    Td = np.empty((5, NT, 8, L))
    for b in range(5):
        x = filt[b][TARGETS][:, :, :, start:start + L][:, train]                  # (NT, nb, 8, L)
        Ws = np.stack([T.trca_filter(x[t]) for t in range(NT)], axis=1)           # (8, NT)
        M[b] = Ws @ Ws.T
        tm = x.mean(axis=1)
        tm = tm - tm.mean(axis=2, keepdims=True)
        Td[b] = tm
        for t in range(NT):
            R = P.basis(L, float(freqs[t]), 5)                                      # (L, 10)
            coef, *_ = np.linalg.lstsq(R, tm[t].T, rcond=None)
            Fo[b, t] = (R @ coef).T
    return M, Fo, Td


def prep_template(M, T_):
    """-> fused template Z = M T (5,NT,8,L) and template energy <M, T T^T> (NT,5)."""
    Z = np.einsum("bij,btjl->btil", M, T_)
    E = np.einsum("btil,btil->tb", T_, Z)
    return Z, E


def scores(M, Z, E, x):
    """x (5,8,L) -> (NT,) weighted score."""
    total = np.zeros(NT)
    for b in range(5):
        xb = x[b]
        num = np.einsum("il,til->t", xb, Z[b])
        den_x = float(np.sum(M[b] * (xb @ xb.T)))
        r = num / np.sqrt(np.maximum(den_x * E[:, b], 1e-30))
        total += W[b] * np.sign(r) * r ** 2
    return total


def build(filt, start, freqs, train):
    """Per scheme and per model: (M, Z, E)."""
    models = {s: [] for s in SCHEMES}
    full = band_models(filt, start, freqs, train, LENGTHS[-1])
    M_full, Fo_full, Td_full = full
    for L in LENGTHS:
        M, Fo, Td = band_models(filt, start, freqs, train, L) if L != LENGTHS[-1] else full
        templates = {
            "fourier": Fo,
            "td_len": Td,
            "td_pref": Td_full[..., :L],
            "blend_len": (1 - ALPHA) * Fo + ALPHA * Td,
            "blend_pref": ((1 - ALPHA) * Fo_full + ALPHA * Td_full)[..., :L],
        }
        for s in SCHEMES:
            Z, E = prep_template(M, templates[s])
            models[s].append((M, Z, E))
    return models


def thresholds(models, filt, start, train):
    """Resubstitution thresholds per scheme and model, exactly like etrca_calib.calibrate (float)."""
    thr = {}
    for s in SCHEMES:
        thr[s] = []
        for m, L in enumerate(LENGTHS):
            M, Z, E = models[s][m]
            best, margins = [], []
            for t_i, t in enumerate(TARGETS):
                for r in train:
                    S = scores(M, Z, E, filt[:, t, r, :, start:start + L])
                    order = np.argsort(S)[::-1]
                    if order[0] == t_i:
                        best.append(S[order[0]])
                        margins.append((S[order[0]] - S[order[1]]) / max(S[order[0]], 1e-30))
            thr[s].append((F_S * float(np.median(best)) if best else 0.0, F_M * float(np.median(margins)) if margins else 0.0))
    return thr


def thresholds_loro(filt, start, freqs, train):
    """Same medians, but every training window is scored by a model that never saw its round (2 of the 3 rounds)."""
    pools = {s: [([], []) for _ in LENGTHS] for s in SCHEMES}
    for held in train:
        sub = [r for r in train if r != held]
        models = build(filt, start, freqs, sub)
        for s in SCHEMES:
            for m, L in enumerate(LENGTHS):
                M, Z, E = models[s][m]
                for t_i, t in enumerate(TARGETS):
                    S = scores(M, Z, E, filt[:, t, held, :, start:start + L])
                    order = np.argsort(S)[::-1]
                    if order[0] == t_i:
                        pools[s][m][0].append(S[order[0]])
                        pools[s][m][1].append((S[order[0]] - S[order[1]]) / max(S[order[0]], 1e-30))
    thr = {}
    for s in SCHEMES:
        thr[s] = [(F_S * float(np.median(b)) if b else 0.0, F_M * float(np.median(g)) if g else 0.0) for b, g in pools[s]]
    return thr


def decide(models, thr, filt, start, t_i, t, r):
    """Run one test window through the checkpoints -> (decided, target, checkpoint index, forced best per checkpoint)."""
    agree, last = 0, -1
    forced = []
    out = None
    for m, L in enumerate(LENGTHS):
        M, Z, E = models[m]
        S = scores(M, Z, E, filt[:, t, r, :, start:start + L])
        order = np.argsort(S)[::-1]
        best, s_best, s_second = int(order[0]), S[order[0]], S[order[1]]
        forced.append(best)
        ts, tm = thr[m]
        gate = s_best > 0 and s_best >= ts and (s_best - s_second) / s_best >= tm
        if out is None:
            agree = (agree + 1 if (agree and last == best) else 1) if gate else 0
            last = best if gate else last
            if gate and agree >= AGREE:
                out = (True, best, m)
    return (out if out else (False, -1, len(LENGTHS) - 1)), forced


def subject(args):
    global ALPHA
    s, offset, ALPHA = args
    notch, bands = F.parse_coefficients()
    filt, start0, freqs = B.load_filtered(s, notch, bands)
    onset = int(round(B.ONSET * FS / B.FS_SRC))
    start = onset + offset
    f28 = [float(freqs[t]) for t in TARGETS]
    res = {c: {"n": 0, "decided": 0, "correct": 0, "len_sum": 0.0, "forced_ok": [0] * len(LENGTHS)} for c in COMBOS}
    for train, test in SPLITS:
        models = build(filt, start, f28, train)
        thr_all = {"resub": thresholds(models, filt, start, train), "loro": thresholds_loro(filt, start, f28, train)}
        for combo in COMBOS:
            sch, mode = combo.split("|")
            thr = thr_all[mode]
            for t_i, t in enumerate(TARGETS):
                for r in test:
                    (dec, tgt, m), forced = decide(models[sch], thr[sch], filt, start, t_i, t, r)
                    d = res[combo]
                    d["n"] += 1
                    d["len_sum"] += LENGTHS[m] / FS
                    if dec:
                        d["decided"] += 1
                        d["correct"] += int(tgt == t_i)
                    for k, f in enumerate(forced):
                        d["forced_ok"][k] += int(f == t_i)
    return res


def itr_bits_per_min(acc, seconds, n=NT):
    if acc <= 0 or seconds <= 0:
        return 0.0
    bits = np.log2(n) + (acc * np.log2(acc) if acc > 0 else 0) + ((1 - acc) * np.log2((1 - acc) / (n - 1)) if acc < 1 else 0)
    return max(bits, 0.0) * 60.0 / seconds


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--subjects", default="1-10")
    ap.add_argument("--offset", type=int, default=122, help="frames after the stimulus onset at which the scored window starts")
    ap.add_argument("--alpha", type=float, default=0.5)
    a = ap.parse_args()
    global ALPHA
    ALPHA = a.alpha
    lo, hi = (int(v) for v in a.subjects.split("-"))
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        per = list(pool.map(subject, [(s, a.offset, a.alpha) for s in range(lo, hi + 1)]))
    out = {"offset_frames": a.offset, "lengths": LENGTHS, "alpha": a.alpha, "subjects": per, "mean": {}}
    print(f"offset {a.offset} frames; 28 targets; 3-round calibration; {len(per)} subjects")
    print("scheme/thresholds   decided  acc|dec  acc(all)  mean_len_s  ITR(b/min,+0.5s)    forced acc at " + " ".join(f"{L / FS:.2f}s" for L in LENGTHS))
    for sch in COMBOS:
        n = sum(p[sch]["n"] for p in per)
        dec = sum(p[sch]["decided"] for p in per)
        cor = sum(p[sch]["correct"] for p in per)
        mlen = sum(p[sch]["len_sum"] for p in per) / n
        forced = [sum(p[sch]["forced_ok"][k] for p in per) / n for k in range(len(LENGTHS))]
        acc_dec = cor / dec if dec else 0.0
        itr = itr_bits_per_min(cor / n, mlen + 0.5)
        out["mean"][sch] = {"decided": dec / n, "acc_decided": acc_dec, "acc_all": cor / n, "mean_len_s": mlen, "itr": itr, "forced": forced, "n": n}
        print(f"{sch:17s}  {100 * dec / n:5.1f}%  {100 * acc_dec:6.1f}%  {100 * cor / n:6.1f}%   {mlen:6.3f}     {itr:7.1f}          " + " ".join(f"{100 * v:5.1f}" for v in forced))
    a.json.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
