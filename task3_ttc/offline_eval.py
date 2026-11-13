"""Task 3 Steps 1-2 (and the method grid used by Step 6): fixed-budget selection on the supplied pools.

For every question, each of the 10 saved orders and N in {1,2,4,8,16,32} (nested prefixes): every selection
method's chosen answer group, correctness, oracle coverage, #distinct answer groups and generated-token cost
(sum of retained token counts of the consumed candidates; FirstAns consumes one). Outcomes are averaged over
orders within a question, then over questions. 95% CIs: 1,000 paired question-level bootstrap resamples.

Outputs: $PA3_OUT/task3/<model>_per_order.parquet (every selection, with candidate & selected rollout IDs)
         results/task3/<model>/per_question.parquet, budget_table.csv, failure_decomposition.csv,
         comparisons_bootstrap.csv, new_sample_utility.csv
Usage:  python -m task3_ttc.offline_eval
"""
import argparse
import os

import numpy as np
import pandas as pd

from common.utils import load_config, out_dir, results_dir
from task3_ttc.data import load_pool
from task3_ttc.scoring import all_selections

COMPARISONS = [  # (A, B): report B - A
    ("FirstAns", "SC"), ("SC", "SelfCert+RWC"), ("SelfCert", "SelfCert+RWC"), ("LogP", "SelfCert"),
    ("SC", "SelfCert"), ("SC", "LogP"), ("SC", "Shortest"),
    ("Vision", "Vision+RWC"), ("VisConf", "VisConf+RWC"), ("SelfCert+RWC", "VisConf+RWC"), ("SC", "VisConf+RWC"),
    ("Vision+RWC", "VisConf+RWC"), ("VisConf", "TokFusion"), ("VisConf+RWC", "TokFusion+RWC"), ("SelfCert", "VisConf")]


def evaluate_model(cfg, mkey):
    blob = load_pool(mkey)
    pool, sp = blob["pool"], blob["splits"]
    vision = cfg["models"][mkey]["kind"] == "mathvista"
    dev = set(sp["dev"])
    budgets = cfg["budgets"]
    per_order, per_q = [], []
    util = np.zeros((2, 32))  # P(new group at step n), P(first correct group appears at step n)
    nq_orders = 0
    for sid in sorted(pool):
        q = pool[sid]
        split = "dev" if sid in dev else "eval"
        acc = {}
        for oi, order in enumerate(sp["orders_positions"][sid]):
            order = np.asarray(order)
            seen, have_correct = set(), False
            for n, i in enumerate(order):
                lab = q["labels"][i]
                if lab not in seen:
                    util[0, n] += 1
                    seen.add(lab)
                if q["correct"][i] and not have_correct:
                    util[1, n] += 1
                    have_correct = True
            nq_orders += 1
            for N in budgets:
                pos = order[:N]
                labs = [q["labels"][i] for i in pos]
                cov = bool(q["correct"][pos].any())
                ndist = len(set(labs))
                sel = all_selections(q, pos, vision)
                for m, lab in sel.items():
                    ok = bool(q["group_correct"][lab])
                    cost = float(q["cost"][pos[0]] if m == "FirstAns" else q["cost"][pos].sum())
                    sel_rid = q["rollout_ids"][pos[labs.index(lab)]]
                    per_order.append((split, sid, oi, N, m, lab, sel_rid, ok, cov, ndist, cost))
                    a = acc.setdefault((N, m), [0, 0, 0, 0, 0, 0])
                    a[0] += ok
                    a[1] += cov
                    a[2] += cov and not ok
                    a[3] += ndist
                    a[4] += cost
                    a[5] += 1
        frac = float(q["correct"].mean())
        for (N, m), a in acc.items():
            k = a[5]
            per_q.append({"split": split, "sample_id": sid, "N": N, "method": m, "acc": a[0] / k,
                          "coverage": a[1] / k, "selection_fail": a[2] / k, "sampling_fail": 1 - a[1] / k,
                          "n_distinct": a[3] / k, "cost": a[4] / k, "pool_correct_frac": frac})
    od = out_dir("task3")
    rd = results_dir("task3", mkey)
    po = pd.DataFrame(per_order, columns=["split", "sample_id", "order", "N", "method", "selected_label",
                                          "selected_rollout_id", "correct", "coverage", "n_distinct", "token_cost"])
    po.to_parquet(os.path.join(od, f"{mkey}_per_order.parquet"))
    pq = pd.DataFrame(per_q)
    pq.to_parquet(os.path.join(rd, "per_question.parquet"))
    pd.DataFrame({"n": np.arange(1, 33), "p_new_group": util[0] / nq_orders,
                  "p_first_correct_appears": util[1] / nq_orders}).to_csv(os.path.join(rd, "new_sample_utility.csv"),
                                                                         index=False)
    summarize(cfg, pq, rd)
    return pq


def summarize(cfg, pq, rd):
    rows = []
    for split in ["eval", "dev"]:
        d = pq[pq.split == split]
        g = d.groupby(["N", "method"]).agg(acc=("acc", "mean"), coverage=("coverage", "mean"),
                                          sampling_fail=("sampling_fail", "mean"),
                                          selection_fail=("selection_fail", "mean"), n_distinct=("n_distinct", "mean"),
                                          mean_token_cost=("cost", "mean"), n_questions=("sample_id", "nunique"))
        g = g.reset_index()
        g.insert(0, "split", split)
        rows.append(g)
    tab = pd.concat(rows)
    tab["selection_gap"] = tab["coverage"] - tab["acc"]
    tab.to_csv(os.path.join(rd, "budget_table.csv"), index=False, float_format="%.5g")
    tab[["split", "N", "method", "acc", "coverage", "sampling_fail", "selection_fail"]].to_csv(
        os.path.join(rd, "failure_decomposition.csv"), index=False, float_format="%.5g")
    # paired question-level bootstrap of accuracy differences (held-out eval)
    d = pq[pq.split == "eval"]
    rng = np.random.default_rng(cfg["seed"])
    qs = sorted(d.sample_id.unique())
    idx = rng.integers(0, len(qs), (cfg["bootstrap"], len(qs)))
    out = []
    for N in cfg["budgets"]:
        piv = d[d.N == N].pivot(index="sample_id", columns="method", values="acc").loc[qs]
        for a, b in COMPARISONS:
            if a in piv and b in piv:
                diff = (piv[b] - piv[a]).values
                bm = diff[idx].mean(1)
                out.append({"N": N, "comparison": f"{b} − {a}", "diff_pp": 100 * diff.mean(),
                            "ci95_lo": 100 * np.quantile(bm, .025), "ci95_hi": 100 * np.quantile(bm, .975)})
    pd.DataFrame(out).to_csv(os.path.join(rd, "comparisons_bootstrap.csv"), index=False, float_format="%.3f")
    print(tab[tab.split == "eval"].pivot(index="method", columns="N", values="acc").round(4).to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task3.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    ap.add_argument("--models", nargs="*", default=["llm", "vlm"])
    a = ap.parse_args()
    cfg = load_config(a.config, a.set)
    for mk in a.models:
        evaluate_model(cfg, mk)
