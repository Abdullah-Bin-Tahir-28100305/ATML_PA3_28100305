"""Task 3 Step 3: what the signals capture (N = 32, all rollouts of a question; held-out eval questions by default,
dev reported too). Gold answers are used here ONLY for retrospective analysis.

* Within-question Spearman correlations among length T, L, K (and V, C, Z, Z_tok for MathVista) and correctness
  (questions with mixed correctness only, for correlations involving correctness).
* Top-vs-bottom quartile: within a question, rank rollouts by a score (8 highest vs 8 lowest; ties by rollout
  order), difference in correctness rate, averaged over questions (all, and mixed-correctness only).
  Length is included both ways (longest-first and shortest-first) as a length-only control.
* MathVista: per-rollout Pearson correlation between calibrated confidence c_t and visual engagement v_t, summarized
  across rollouts / questions and by rollout correctness; numerical check of Z_tok - Z = Cov_t(c, v).
* Fixed illustrative trajectories (rule in pick_illustrative) with token-level c_t and v_t plots.
Outputs under results/task3/<model>/signals/.
Usage:  python -m task3_ttc.signals
"""
import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from common.utils import load_config, results_dir, save_json
from task3_ttc.data import load_pool
from task3_ttc.scoring import all_selections, visconf_parts


def rollout_table(q, vision):
    pos = np.arange(32)
    d = {"T": q["T"].astype(float), "L": q["L"], "K": q["K"], "negT": -q["T"].astype(float),
         "correct": q["correct"].astype(float)}
    vc = None
    if vision:
        vc = visconf_parts(q, pos)
        d.update({"V": q["V"], "C": vc["C"], "Z": vc["Z"], "Ztok": vc["Ztok"]})
    return pd.DataFrame(d), vc


def quartile_diff(score, correct):
    s = np.where(np.isfinite(score), score, -np.inf)
    order = np.argsort(-s, kind="stable")  # stable -> ties broken by rollout position
    return correct[order[:8]].mean() - correct[order[-8:]].mean()


def run(cfg, mkey):
    blob = load_pool(mkey)
    pool, sp = blob["pool"], blob["splits"]
    vision = cfg["models"][mkey]["kind"] == "mathvista"
    od = results_dir("task3", mkey, "signals")
    scores = ["T", "negT", "L", "K"] + (["V", "C", "Z", "Ztok"] if vision else [])
    res = {}
    for split in ["eval", "dev"]:
        corr_rows, quart_rows, cv_rows, cov_err = [], [], [], 0.0
        for sid in sp[split]:
            q = pool[sid]
            df, vc = rollout_table(q, vision)
            mixed = 0 < df.correct.mean() < 1
            for s in scores:
                quart_rows.append({"sample_id": sid, "score": s, "mixed": mixed,
                                   "top_minus_bottom": quartile_diff(df[s].values, df.correct.values)})
                if mixed:
                    x = df[s].replace(-np.inf, np.nan)
                    corr_rows.append({"sample_id": sid, "pair": f"{s}~correct",
                                      "rho": spearmanr(x, df.correct, nan_policy="omit")[0]})
            for a in ["L", "K"] + (["V", "C", "Z"] if vision else []):
                x = df[a].replace(-np.inf, np.nan)
                corr_rows.append({"sample_id": sid, "pair": f"T~{a}", "rho": spearmanr(df["T"], x, nan_policy="omit")[0]})
            if vision:
                for i in range(32):
                    c, v = vc["c_tokens"][i], q["v"][i]
                    if len(c) > 2 and np.std(c) > 0 and np.std(v) > 0:
                        r = float(np.corrcoef(c, v)[0, 1])
                        cov = float(np.mean(c * v) - np.mean(c) * np.mean(v))
                        cov_err = max(cov_err, abs((vc["Ztok"][i] - vc["Z"][i]) - cov))
                    else:
                        r, cov = np.nan, np.nan
                    cv_rows.append({"sample_id": sid, "rollout": i, "r_cv": r, "cov_cv": cov,
                                    "correct": bool(q["correct"][i]), "T": int(q["T"][i])})
        C = pd.DataFrame(corr_rows)
        Q = pd.DataFrame(quart_rows)
        out = {"spearman_mean_over_questions": C.groupby("pair").rho.agg(["mean", "median", "count"]).round(4).to_dict("index"),
               "quartile_top_minus_bottom_all": Q.groupby("score").top_minus_bottom.mean().round(4).to_dict(),
               "quartile_top_minus_bottom_mixed": Q[Q.mixed].groupby("score").top_minus_bottom.mean().round(4).to_dict(),
               "n_questions": len(sp[split]), "n_mixed": int(Q[Q.score == "T"].mixed.sum())}
        C.to_csv(os.path.join(od, f"spearman_{split}.csv"), index=False)
        Q.to_csv(os.path.join(od, f"quartiles_{split}.csv"), index=False)
        if vision:
            V = pd.DataFrame(cv_rows)
            V.to_csv(os.path.join(od, f"conf_vision_corr_{split}.csv"), index=False)
            perq = V.groupby("sample_id").r_cv.mean()
            out["conf_vision"] = {
                "rollout_level": V.r_cv.describe().round(4).to_dict(),
                "question_mean_level": perq.describe().round(4).to_dict(),
                "by_correct": V.groupby("correct").r_cv.mean().round(4).to_dict(),
                "mean_cov": float(V.cov_cv.mean()),
                "max_abs_err_Ztok_minus_Z_vs_cov": cov_err}
        res[split] = out
    save_json(res, os.path.join(od, "signals_summary.json"))
    if vision:
        plot_illustrative(cfg, pool, sp, od)
    print(res["eval"])


def pick_illustrative(pool, sp, k=3):
    """Rule: first k held-out MathVista questions (split order) whose N=32 pool has both correct and incorrect
    rollouts AND where VisConf (highest) and self-consistency pick different answers at N=32 (natural rollout
    order). For each: the VisConf-top rollout and the earliest correct rollout are plotted."""
    picks = []
    for sid in sp["eval"]:
        q = pool[sid]
        if not (0 < q["correct"].mean() < 1):
            continue
        sel = all_selections(q, np.arange(32), vision=True)
        if sel["VisConf"] != sel["SC"]:
            picks.append(sid)
        if len(picks) == k:
            break
    return picks


def plot_illustrative(cfg, pool, sp, od, w=25):
    picks = pick_illustrative(pool, sp)
    fig, axes = plt.subplots(len(picks), 2, figsize=(13, 3.2 * len(picks)), squeeze=False)
    info = []
    for r, sid in enumerate(picks):
        q = pool[sid]
        vc = visconf_parts(q, np.arange(32))
        top = int(np.nanargmax(np.where(np.isfinite(vc["Z"]), vc["Z"], -np.inf)))
        corr = int(np.argmax(q["correct"]))
        for c, (i, name) in enumerate([(top, "VisConf-top"), (corr, "earliest correct")]):
            ct, vt = vc["c_tokens"][i], q["v"][i]
            sm = lambda x: np.convolve(x, np.ones(w) / w, "valid") if len(x) > w else x
            ax = axes[r, c]
            ax.plot(sm(ct), label="c_t (calibrated conf.)")
            ax2 = ax.twinx()
            ax2.plot(sm(vt), c="C1", label="v_t (visual engagement)")
            ax.set_title(f"{sid} | {name} rollout {q['rollout_ids'][i]} | correct={bool(q['correct'][i])} | "
                         f"ans={q['labels'][i]} gold={q.get('gold_norm')}", fontsize=8)
            ax.set_xlabel(f"generated token ({w}-token moving avg)")
            if r == 0 and c == 0:
                ax.legend(loc="upper left", fontsize=7)
                ax2.legend(loc="upper right", fontsize=7)
            info.append({"sample_id": sid, "which": name, "rollout_id": q["rollout_ids"][i],
                         "correct": bool(q["correct"][i]), "C": float(vc["C"][i]), "V": float(q["V"][i]),
                         "Z": float(vc["Z"][i]), "Ztok": float(vc["Ztok"][i]), "T": int(q["T"][i])})
    fig.tight_layout()
    fig.savefig(os.path.join(od, "illustrative_trajectories.png"), dpi=140)
    save_json({"rule": pick_illustrative.__doc__, "examples": info}, os.path.join(od, "illustrative_trajectories.json"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task3.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    ap.add_argument("--models", nargs="*", default=["llm", "vlm"])
    a = ap.parse_args()
    cfg = load_config(a.config, a.set)
    for mk in a.models:
        run(cfg, mk)
