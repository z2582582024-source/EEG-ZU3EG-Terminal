import json
runs = [("offset 122 (deployed), alpha 0.5", "p0_off122.json"), ("offset 122, alpha 0.25", "p0_off122_a0.25.json"), ("offset 122, alpha 0.75", "p0_off122_a0.75.json"),
        ("offset 61 (0.25 s), alpha 0.5", "p0_off61.json"), ("offset 34 (0.14 s), alpha 0.5", "p0_off34.json")]
for name, f in runs:
    d = json.load(open(f, encoding="utf-8"))
    m = d["mean"]
    a, b = m["fourier|loro"], m["blend_pref|loro"]
    diff = [100 * (y - x) for x, y in zip(a["forced"], b["forced"])]
    print(f"{name:34s} forced diff by length (0.25 0.37 0.50 0.75 1.00 1.25 1.50 2.00 s): " + " ".join(f"{v:+5.1f}" for v in diff) + f"  mean {sum(diff)/len(diff):+.2f}"
          f" | dynamic acc(all) {100*a['acc_all']:.1f} -> {100*b['acc_all']:.1f}  len {a['mean_len_s']:.3f} -> {b['mean_len_s']:.3f}  ITR {a['itr']:.0f} -> {b['itr']:.0f}")
d = json.load(open("p0_off122.json", encoding="utf-8"))
for half, rng in (("S1-S5", range(0, 5)), ("S6-S10", range(5, 10))):
    out = []
    for c in ("fourier|loro", "blend_pref|loro"):
        n = sum(d["subjects"][i][c]["n"] for i in rng)
        out.append([sum(d["subjects"][i][c]["forced_ok"][k] for i in rng) / n for k in range(8)])
    print(half, "deployed offset, blend(0.5) - fourier forced diff:", " ".join(f"{100*(y-x):+5.1f}" for x, y in zip(*out)))
d0 = d["mean"]
print("n windows per scheme:", d0["fourier|loro"]["n"])
print("deployed (fourier|resub) vs fourier|loro: decided %.1f -> %.1f, acc|decided %.1f -> %.1f, acc(all) %.1f -> %.1f, mean len %.3f -> %.3f" % (
    100*d0["fourier|resub"]["decided"], 100*d0["fourier|loro"]["decided"], 100*d0["fourier|resub"]["acc_decided"], 100*d0["fourier|loro"]["acc_decided"],
    100*d0["fourier|resub"]["acc_all"], 100*d0["fourier|loro"]["acc_all"], d0["fourier|resub"]["mean_len_s"], d0["fourier|loro"]["mean_len_s"]))
