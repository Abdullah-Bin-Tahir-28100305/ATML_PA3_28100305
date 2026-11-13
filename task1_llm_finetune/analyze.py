"""Task 1 analysis: builds every table/figure listed under "Required Evidence".

Outputs (results/task1/analysis/):
  benchmark_table.{csv,md}        7 conditions x 4 benchmarks, primary + raw scores, change vs baseline (pp)
  gsm8k_breakdown.csv             EM, strict EM, extraction rate, truncation rate, EM among extractable & untruncated,
                                  transitions vs baseline (correct->wrong etc.) -> separates extraction/truncation
                                  from correctness changes
  controlled_comparisons.csv      LoRA4 vs LoRA16, FullFT vs L2SP, LoRA16 vs OPLoRA16, FullFT vs LoRA16, FullFT mid
                                  vs final: score differences (pp) with paired per-example bootstrap 95% CIs
  loss_curves.png                 SFT loss of the 5 runs vs optimizer update (+ LR inset)
  l2sp_penalty.png                Omega, lambda*Omega, SFT and total objective for L2-SP
  regularization.csv              final displacement ||theta-theta0|| (Full FT mid/final, L2-SP), final Omega
  resources.{csv,md}              trainable params, training time, peak GPU memory, checkpoint/export sizes, overhead
  diagnostics_table.csv / diagnostics.png   rho_k, eta, R(math), R(nonmath)
  retention_vs_adaptation.png     GSM8K gain vs mean retention change scatter
Usage:  python -m task1_llm_finetune.analyze
"""
import argparse
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common.utils import load_config, load_json, read_jsonl, results_dir

COND_ORDER = ["baseline", "full_ft_mid", "full_ft", "l2sp", "lora4", "lora16", "oplora16"]
VARIANTS = ["full_ft", "l2sp", "lora4", "lora16", "oplora16"]
LABEL = {"baseline": "Baseline", "full_ft_mid": "Full FT (mid)", "full_ft": "Full FT", "l2sp": "Full FT + L2-SP",
         "lora4": "LoRA-4", "lora16": "LoRA-16", "oplora16": "OPLoRA-16"}
BENCH = [("gsm8k", "flexible_em", "GSM8K EM (flex)"), ("hellaswag", "acc_norm", "HellaSwag acc_norm"),
         ("boolq", "acc", "BoolQ acc"), ("arc_easy", "acc_norm", "ARC-E acc_norm")]
PER_EX = {"gsm8k": ("gsm8k_rows.jsonl", "exact_match_flexible-extract"), "hellaswag": ("hellaswag_rows.jsonl", "acc_norm"),
          "boolq": ("boolq_rows.jsonl", "acc"), "arc_easy": ("arc_easy_rows.jsonl", "acc_norm")}


def R(*p):
    return results_dir("task1", *p)


def load_summaries():
    out = {}
    for c in COND_ORDER:
        f = os.path.join(R("eval", c), "summary.json")
        if os.path.exists(f):
            out[c] = load_json(f)
    return out


def per_example(cond, bench):
    fn, key = PER_EX[bench]
    rows = read_jsonl(os.path.join(R("eval", cond), fn))
    return np.array([float(r.get(key) or 0.0) for r in sorted(rows, key=lambda r: r["doc_id"])])


def paired_boot(a, b, n=1000, seed=6304):
    d = b - a
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), (n, len(d)))
    m = d[idx].mean(1)
    return 100 * d.mean(), 100 * np.quantile(m, .025), 100 * np.quantile(m, .975)


def benchmark_table(S):
    rows = []
    base = S.get("baseline")
    for c in COND_ORDER:
        if c not in S:
            continue
        r = {"condition": LABEL[c]}
        for b, k, name in BENCH:
            v = S[c][b][k]
            r[name] = 100 * v
            if base:
                r[f"Δ {name} (pp)"] = 100 * (v - base[b][k])
        for b in ["hellaswag", "arc_easy"]:
            r[f"{b} raw acc"] = 100 * S[c][b]["acc"]
        r["GSM8K strict EM"] = 100 * S[c]["gsm8k"]["strict_em"]
        if base:
            r["Δ mean retention (pp)"] = np.mean([r[f"Δ {n} (pp)"] for _, _, n in BENCH[1:]])
        rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(R("analysis"), "benchmark_table.csv"), index=False, float_format="%.2f")
    with open(os.path.join(R("analysis"), "benchmark_table.md"), "w") as f:
        f.write(df.to_markdown(index=False, floatfmt=".2f"))
    return df


def gsm8k_breakdown(S):
    rows = []
    base_rows = {r["doc_id"]: r for r in read_jsonl(os.path.join(R("eval", "baseline"), "gsm8k_rows.jsonl"))}
    for c in COND_ORDER:
        if c not in S:
            continue
        rr = {r["doc_id"]: r for r in read_jsonl(os.path.join(R("eval", c), "gsm8k_rows.jsonl"))}
        g = S[c]["gsm8k"]
        d = {"condition": LABEL[c], "EM_flex": 100 * g["flexible_em"], "EM_strict": 100 * g["strict_em"],
             "extraction_rate": 100 * g["extraction_rate"], "truncation_rate": 100 * g["truncation_rate"],
             "EM_among_extractable_untruncated": 100 * (g["em_among_extractable_nontruncated"] or 0),
             "mean_response_tokens": g["mean_response_tokens"]}
        if base_rows and c != "baseline":
            tr = {"c->c": 0, "c->w": 0, "w->c": 0, "w->w": 0, "c->w_due_to_unextractable": 0, "c->w_truncated": 0}
            for did, r in rr.items():
                b = base_rows[did]
                bc, cc = bool(b.get("exact_match_flexible-extract")), bool(r.get("exact_match_flexible-extract"))
                tr[f"{'c' if bc else 'w'}->{'c' if cc else 'w'}"] += 1
                if bc and not cc:
                    tr["c->w_due_to_unextractable"] += int(not r["extractable"])
                    tr["c->w_truncated"] += int(r["truncated"])
            d.update(tr)
        rows.append(d)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(R("analysis"), "gsm8k_breakdown.csv"), index=False, float_format="%.2f")
    return df


def controlled_comparisons(S):
    pairs = [("lora4", "lora16", "rank"), ("full_ft", "l2sp", "explicit regularization"),
             ("lora16", "oplora16", "orthogonal protection"), ("full_ft", "lora16", "full vs low-rank"),
             ("full_ft_mid", "full_ft", "Full FT progression"), ("baseline", "full_ft", "Full FT vs base")]
    rows = []
    for a, b, what in pairs:
        if a not in S or b not in S:
            continue
        for bench, _, name in BENCH:
            m, lo, hi = paired_boot(per_example(a, bench), per_example(b, bench))
            rows.append({"comparison": f"{LABEL[b]} − {LABEL[a]}", "factor": what, "benchmark": name,
                         "diff_pp": m, "ci95_lo": lo, "ci95_hi": hi})
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(R("analysis"), "controlled_comparisons.csv"), index=False, float_format="%.2f")
    return df


def smooth(x, w=25):
    x = np.asarray(x, float)
    if len(x) < w:
        return x
    c = np.convolve(x, np.ones(w) / w, mode="valid")
    return np.concatenate([np.full(w - 1, np.nan), c])


def loss_plots():
    fig, ax = plt.subplots(1, 2, figsize=(11, 4))
    logs = {}
    for v in VARIANTS:
        f = os.path.join(R("runs"), f"{v}_train_log.jsonl")
        if os.path.exists(f):
            logs[v] = pd.DataFrame(read_jsonl(f))
    for v, df in logs.items():
        ax[0].plot(df["update"], df["sft_loss"], alpha=.15)
        ax[0].plot(df["update"], smooth(df["sft_loss"]), label=LABEL[v], color=ax[0].lines[-1].get_color())
        ax[1].plot(df["update"], df["lr"], label=LABEL[v])
    ax[0].set(xlabel="optimizer update", ylabel="SFT loss (response tokens, nats/token)",
              title="SFT loss (raw, light; 25-update moving avg, dark)")
    ax[0].axvline(625, ls=":", c="gray")
    ax[0].legend()
    ax[1].set(xlabel="optimizer update", ylabel="learning rate", yscale="log", title="LR schedule")
    ax[1].legend()
    fig.tight_layout()
    fig.savefig(os.path.join(R("analysis"), "loss_curves.png"), dpi=160)
    if "l2sp" in logs:
        df = logs["l2sp"]
        fig, ax = plt.subplots(1, 2, figsize=(11, 4))
        ax[0].plot(df["update"], df["omega"], label="Ω")
        ax[0].set(xlabel="optimizer update", ylabel="Ω = ½‖θ−θ0‖²", title="L2-SP penalty")
        ax[1].plot(df["update"], smooth(df["sft_loss"]), label="SFT loss")
        ax[1].plot(df["update"], smooth(df["total_loss"]), label="total = SFT + λΩ")
        ax[1].plot(df["update"], df["lambda_omega"], label="λΩ")
        if "full_ft" in logs:
            ax[1].plot(logs["full_ft"]["update"], smooth(logs["full_ft"]["sft_loss"]), ls="--", label="Full FT SFT")
        ax[1].set(xlabel="optimizer update", ylabel="nats/token")
        ax[1].legend()
        fig.tight_layout()
        fig.savefig(os.path.join(R("analysis"), "l2sp_penalty.png"), dpi=160)
    return logs


def regularization_and_resources(cfg, logs):
    rows, reg = [], []
    for v in VARIANTS:
        f = os.path.join(R("runs"), f"{v}_run_metrics.json")
        if not os.path.exists(f):
            continue
        m = load_json(f)
        ex = m.get("exports", {})
        fin = ex.get("1250", {})
        rows.append({"method": LABEL[v], "trainable_params": m["trainable_params"],
                     "train_hours": m["train_hours"], "peak_gpu_mem_GB": m["peak_gpu_mem_mb_max_over_sessions"] / 1024,
                     "recovery_ckpt_GB": max(m.get("recovery_ckpt_bytes", {"0": 0}).values()) / 2**30,
                     "inference_export_MB": fin.get("export_bytes", 0) / 2**20,
                     "adapter_file_MB": fin.get("adapter_file_bytes", 0) / 2**20,
                     "export_sec": fin.get("export_sec"), "skipped_updates": m.get("n_skipped"),
                     "l2sp_ref_MB": m.get("l2sp_reference_bytes", 0) / 2**20,
                     "l2sp_penalty_sec": m.get("l2sp_penalty_sec"),
                     "oplora_basis_MB": m.get("basis_storage_bytes", 0) / 2**20,
                     "oplora_basis_file_MB": fin.get("basis_file_bytes", 0) / 2**20,
                     "oplora_svd_sec": m.get("basis_preprocessing_sec"), "gpu": m.get("gpu")})
        if v in ("full_ft", "l2sp"):
            for u, e in sorted(ex.items(), key=lambda t: int(t[0])):
                reg.append({"method": LABEL[v], "update": int(u), "displacement_l2": e.get("displacement_l2"),
                            "omega_from_displacement": 0.5 * e.get("displacement_l2", 0) ** 2,
                            "omega_logged_last_update": e.get("omega"),
                            "lambda_omega": cfg["variants"][v].get("l2sp_lambda", 0) *
                                            0.5 * e.get("displacement_l2", 0) ** 2})
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(R("analysis"), "resources.csv"), index=False, float_format="%.4g")
    with open(os.path.join(R("analysis"), "resources.md"), "w") as f:
        f.write(df.to_markdown(index=False, floatfmt=".4g"))
    pd.DataFrame(reg).to_csv(os.path.join(R("analysis"), "regularization.csv"), index=False, float_format="%.5g")
    return df


def diagnostics_figure():
    f = os.path.join(R("diagnostics"), "diagnostics.json")
    if not os.path.exists(f):
        return
    d = load_json(f)
    W = pd.DataFrame(d["weight"])
    A = pd.DataFrame([r for r in d["activation"] if r["set"] in ("math", "nonmath")])
    piv = A.pivot_table(index=["ckpt", "layer"], columns="set", values="R_mean").reset_index()
    T = W.merge(piv, on=["ckpt", "layer"])
    T["R_math/nonmath"] = T["math"] / T["nonmath"]
    T.to_csv(os.path.join(R("analysis"), "diagnostics_table.csv"), index=False, float_format="%.4g")
    ck = [c for c in ["full_ft_mid", "full_ft", "l2sp", "lora4", "lora16", "oplora16"] if c in set(T.ckpt)]
    fig, ax = plt.subplots(1, 3, figsize=(15, 4))
    layers = sorted(T.layer.unique())
    w = .8 / len(layers)
    x = np.arange(len(ck))
    for i, li in enumerate(layers):
        s = T[T.layer == li].set_index("ckpt").loc[ck]
        ax[0].bar(x + i * w, s["rho_k"], w, label=f"layer {li}")
        ax[1].bar(x + i * w, s["eta"], w, label=f"layer {li}")
        ax[2].bar(x + i * w, s["math"], w, label=f"L{li} math")
        ax[2].bar(x + i * w, s["nonmath"], w, fill=False, hatch="//", edgecolor="k", label=f"L{li} non-math" if i == 0 else None)
    for a, t in zip(ax, ["ρ_k (k=128): fraction of ‖ΔW‖² in protected output subspace",
                         "η = ‖ΔW‖_F / ‖W0‖_F (log)", "mean R(z): math (solid) vs non-math (hatched) (log)"]):
        a.set_xticks(x + w)
        a.set_xticklabels([LABEL[c] for c in ck], rotation=30, ha="right")
        a.set_title(t, fontsize=9)
    ax[1].set_yscale("log")
    ax[2].set_yscale("log")
    ax[0].legend(fontsize=7)
    ax[2].legend(fontsize=6)
    fig.tight_layout()
    fig.savefig(os.path.join(R("analysis"), "diagnostics.png"), dpi=160)


def tradeoff_plot(df):
    if "Δ GSM8K EM (flex) (pp)" not in df:
        return
    fig, ax = plt.subplots(figsize=(5.5, 4.5))
    for _, r in df.iterrows():
        ax.scatter(r["Δ GSM8K EM (flex) (pp)"], r["Δ mean retention (pp)"])
        ax.annotate(r["condition"], (r["Δ GSM8K EM (flex) (pp)"], r["Δ mean retention (pp)"]), fontsize=8)
    ax.axhline(0, c="gray", lw=.5)
    ax.axvline(0, c="gray", lw=.5)
    ax.set(xlabel="Δ GSM8K flexible EM vs baseline (pp)", ylabel="mean Δ HellaSwag/BoolQ/ARC-E (pp)",
           title="Adaptation vs retention")
    fig.tight_layout()
    fig.savefig(os.path.join(R("analysis"), "retention_vs_adaptation.png"), dpi=160)


def main(cfg):
    S = load_summaries()
    if S:
        df = benchmark_table(S)
        print(df.to_string())
        if "baseline" in S:
            print(gsm8k_breakdown(S).to_string())
        print(controlled_comparisons(S).to_string())
        tradeoff_plot(df)
    logs = loss_plots()
    print(regularization_and_resources(cfg, logs).to_string())
    diagnostics_figure()
    print("figures/tables in", R("analysis"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task1.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    main(load_config(a.config, a.set))
