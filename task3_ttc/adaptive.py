"""Task 3 Step 5: adaptive sampling (offline simulation of stopping BETWEEN completed rollouts).

Rollouts are revealed in the 10 saved orders; stopping is checked after n in {4, 8, 16}, else stop at 32. Both
controllers return the current self-consistency answer (only the stopping rule differs).
  agreement     : stop if a unique, non-NO_ANSWER leading answer has >= tau * n votes
  quality-aware : agreement condition AND SC winner == RWC winner AND RWC margin >= gamma
                  (RWC with self-certainty for MATH, VisConf for MathVista; scores / VisConf calibration / ranks use
                  ONLY the revealed candidates). Margin = (top vote - runner-up vote) / total vote weight.
Selection on the DEV split: the lowest-mean-token-cost setting whose dev accuracy is no more than 1 pp below fixed-32
self-consistency (averaged over the 10 orders); ties -> higher accuracy, then smaller tau, then smaller gamma.
The always-32 policy is the fallback. Settings are frozen before evaluating on the held-out split.

Outputs: results/task3/<model>/adaptive/{grid_dev.csv, selected.json, heldout_summary.csv, stop_distribution.csv,
         early_stop_errors.csv, by_pool_correct_frac.csv, fixed_vs_adaptive.png}
Usage:  python -m task3_ttc.adaptive
"""
import argparse
import itertools
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common.utils import load_config, results_dir, save_json
from task3_ttc.answers import NO_ANSWER
from task3_ttc.data import load_pool
from task3_ttc.scoring import quality_scores, rwc, rwc_margin, vote


def checkpoint_stats(q, order, ns, kind):
    """For each check point n: SC winner, uniqueness of the lead, lead fraction, RWC winner + margin, cost."""
    st = {}
    for n in ns + [32]:
        pos = np.asarray(order[:n])
        labels = [q["labels"][i] for i in pos]
        win, W, _ = vote(labels)
        top = W[win]
        unique = sum(1 for v in W.values() if v == top) == 1
        rw, RW = rwc(labels, quality_scores(q, pos, kind))
        st[n] = {"sc": win, "unique": unique, "frac": top / n, "rwc": rw, "margin": rwc_margin(RW),
                 "cost": float(q["cost"][pos].sum()), "sc_correct": bool(q["group_correct"][win])}
    return st


def simulate(stats, ns, tau, gamma, quality):
    """stats: per (question, order) checkpoint dicts. Returns (stop_n, correct, cost)."""
    for n in ns:
        s = stats[n]
        ok = s["unique"] and s["sc"] != NO_ANSWER and s["frac"] >= tau
        if quality:
            ok = ok and s["sc"] == s["rwc"] and s["margin"] >= gamma
        if ok:
            return n, s["sc_correct"], s["cost"]
    s = stats[32]
    return 32, s["sc_correct"], s["cost"]


def policy_eval(all_stats, qids, ns, tau, gamma, quality):
    """Average over orders within question, then over questions."""
    accs, costs, stops = [], [], []
    for sid in qids:
        r = [simulate(st, ns, tau, gamma, quality) for st in all_stats[sid]]
        accs.append(np.mean([x[1] for x in r]))
        costs.append(np.mean([x[2] for x in r]))
        stops.append(np.mean([x[0] for x in r]))
    return float(np.mean(accs)), float(np.mean(costs)), float(np.mean(stops)), np.array(accs)


def run(cfg, mkey):
    blob = load_pool(mkey)
    pool, sp = blob["pool"], blob["splits"]
    kind = cfg["models"][mkey]["kind"]
    C = cfg["adaptive"]
    ns = C["check_points"]
    od = results_dir("task3", mkey, "adaptive")
    all_stats = {sid: [checkpoint_stats(pool[sid], o, ns, kind) for o in sp["orders_positions"][sid]] for sid in pool}

    def fixed(qids, N):
        a = [np.mean([pool[s]["group_correct"][vote([pool[s]["labels"][i] for i in o[:N]])[0]]
                      for o in sp["orders_positions"][s]]) for s in qids]
        c = [np.mean([pool[s]["cost"][np.asarray(o[:N])].sum() for o in sp["orders_positions"][s]]) for s in qids]
        return float(np.mean(a)), float(np.mean(c)), np.array(a)

    dev_fixed32 = fixed(sp["dev"], 32)[0]
    grid, selected = [], {}
    for family, quality in [("agreement", False), ("quality_aware", True)]:
        cands = [{"family": family, "tau": None, "gamma": None, "policy": "fixed32", "dev_acc": dev_fixed32,
                  "dev_cost": fixed(sp["dev"], 32)[1], "dev_mean_rollouts": 32.0}]
        for tau, gamma in itertools.product(C["tau"], C["gamma"] if quality else [None]):
            a, c, s, _ = policy_eval(all_stats, sp["dev"], ns, tau, gamma or 0.0, quality)
            cands.append({"family": family, "tau": tau, "gamma": gamma, "policy": "adaptive", "dev_acc": a,
                          "dev_cost": c, "dev_mean_rollouts": s})
        grid += cands
        ok = [x for x in cands if x["dev_acc"] >= dev_fixed32 - C["tolerance_pp"] / 100 - 1e-12]
        best = sorted(ok, key=lambda x: (x["dev_cost"], -x["dev_acc"], x["tau"] if x["tau"] is not None else 9,
                                         x["gamma"] if x["gamma"] is not None else 9))[0]
        selected[family] = best
    pd.DataFrame(grid).to_csv(os.path.join(od, "grid_dev.csv"), index=False, float_format="%.5g")
    save_json({"dev_fixed32_sc_acc": dev_fixed32, "selected": selected,
               "rule": "lowest dev cost with dev acc >= fixed-32 SC - 1pp; ties: higher acc, smaller tau, gamma"},
              os.path.join(od, "selected.json"))

    # ------------------ held-out evaluation with frozen settings
    ev = sp["eval"]
    rows, dist_rows, err_rows = [], [], []
    for N in cfg["budgets"]:
        a, c, _ = fixed(ev, N)
        rows.append({"policy": f"fixed-{N} SC", "acc": a, "mean_token_cost": c, "mean_rollouts": N})
    for family, sel in selected.items():
        quality = family == "quality_aware"
        if sel["policy"] == "fixed32":
            a, c, _ = fixed(ev, 32)
            rows.append({"policy": f"{family} (fallback fixed-32)", "acc": a, "mean_token_cost": c, "mean_rollouts": 32})
            continue
        a, c, s, per_q = policy_eval(all_stats, ev, ns, sel["tau"], sel["gamma"] or 0.0, quality)
        rows.append({"policy": f"{family} tau={sel['tau']} gamma={sel['gamma']}", "acc": a, "mean_token_cost": c,
                     "mean_rollouts": s})
        stops = []
        for sid in ev:
            frac = float(pool[sid]["correct"].mean())
            for oi, st in enumerate(all_stats[sid]):
                n, ok, cost = simulate(st, ns, sel["tau"], sel["gamma"] or 0.0, quality)
                stops.append({"family": family, "sample_id": sid, "order": oi, "stop_n": n, "correct": ok,
                              "cost": cost, "fixed32_correct": st[32]["sc_correct"], "pool_correct_frac": frac,
                              "lead_frac_at_4": st[4]["frac"]})
                if n < 32 and not ok:
                    err_rows.append({"family": family, "sample_id": sid, "order": oi, "stop_n": n,
                                     "stopped_answer": st[n]["sc"], "fixed32_answer": st[32]["sc"],
                                     "fixed32_would_recover": st[32]["sc_correct"], "lead_frac": st[n]["frac"],
                                     "rwc_margin": st[n]["margin"], "pool_correct_frac": frac,
                                     "gold": pool[sid].get("gold_norm", pool[sid]["gold"])})
        S = pd.DataFrame(stops)
        dist = S.stop_n.value_counts(normalize=True).sort_index()
        for n, p in dist.items():
            dist_rows.append({"family": family, "stop_n": int(n), "fraction": float(p)})
        S["frac_bin"] = pd.cut(S.pool_correct_frac, [-.01, 0, .25, .5, .75, .99, 1.0],
                               labels=["0", "(0,.25]", "(.25,.5]", "(.5,.75]", "(.75,1)", "1"])
        S.groupby("frac_bin", observed=False).agg(mean_stop=("stop_n", "mean"), acc=("correct", "mean"),
                                                  fixed32_acc=("fixed32_correct", "mean"),
                                                  n=("stop_n", "size")).to_csv(
            os.path.join(od, f"by_pool_correct_frac_{family}.csv"))
        S.groupby(pd.cut(S.lead_frac_at_4, [0, .5, .75, .99, 1.0]), observed=False).agg(
            mean_stop=("stop_n", "mean"), acc=("correct", "mean"), n=("stop_n", "size")).to_csv(
            os.path.join(od, f"by_agreement_at4_{family}.csv"))
        S.to_csv(os.path.join(od, f"stops_{family}.csv"), index=False)
    H = pd.DataFrame(rows)
    H.to_csv(os.path.join(od, "heldout_summary.csv"), index=False, float_format="%.5g")
    pd.DataFrame(dist_rows).to_csv(os.path.join(od, "stop_distribution.csv"), index=False)
    E = pd.DataFrame(err_rows)
    E.to_csv(os.path.join(od, "early_stop_errors.csv"), index=False)
    if len(E):
        E.groupby("family").agg(n_incorrect_early=("sample_id", "size"),
                                fixed32_would_recover=("fixed32_would_recover", "mean")).to_csv(
            os.path.join(od, "early_stop_errors_summary.csv"))
    # accuracy-cost plot: fixed budgets vs adaptive policies
    fig, ax = plt.subplots(figsize=(6, 4.5))
    fx = H[H.policy.str.startswith("fixed")]
    ax.plot(fx.mean_token_cost, 100 * fx.acc, "o-", label="fixed-N self-consistency")
    for _, r in fx.iterrows():
        ax.annotate(f"N={int(r.mean_rollouts)}", (r.mean_token_cost, 100 * r.acc), fontsize=7)
    for _, r in H[~H.policy.str.startswith("fixed")].iterrows():
        ax.scatter(r.mean_token_cost, 100 * r.acc, marker="*", s=150, label=r.policy)
    ax.set(xscale="log", xlabel="mean generated-token cost per question (log)", ylabel="accuracy (%)",
           title=f"{mkey}: adaptive vs fixed budgets (held-out)")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(os.path.join(od, "fixed_vs_adaptive.png"), dpi=150)
    print(H.to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task3.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    ap.add_argument("--models", nargs="*", default=["llm", "vlm"])
    a = ap.parse_args()
    cfg = load_config(a.config, a.set)
    for mk in a.models:
        run(cfg, mk)
