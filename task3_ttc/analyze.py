"""Task 3 Step 6: compute allocation & failure analysis + qualitative case selection.

Figures/tables (results/task3/<model>/analysis and results/task3/qualitative_cases.md):
  accuracy_vs_N.png / accuracy_vs_cost.png   every method + oracle coverage (held-out eval)
  coverage_gap.png                           oracle coverage, SC / best-method accuracy, selection gap, sampling vs
                                             selection failure decomposition (stacked) vs N
  diversity.png                              #distinct answer groups vs N, P(new group at sample n),
                                             P(first correct group appears at sample n)
  method_table_N{8,32}.md                    compact method comparison (+ bootstrap CIs from offline_eval)
  qualitative_cases.md                       aligned examples of selection disagreement, revision, premature stopping
                                             (selection rules stated in the file)
Usage:  python -m task3_ttc.analyze
"""
import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common.utils import load_config, read_jsonl, results_dir
from task3_ttc.data import load_pool
from task3_ttc.scoring import all_selections


def plots(mkey):
    R = results_dir("task3", mkey)
    A = results_dir("task3", mkey, "analysis")
    T = pd.read_csv(os.path.join(R, "budget_table.csv"))
    T = T[T.split == "eval"]
    methods = list(dict.fromkeys(T.method))
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.8))
    cov = T[T.method == "SC"].sort_values("N")
    for m in methods:
        d = T[T.method == m].sort_values("N")
        ls = "--" if "RWC" in m else "-"
        ax[0].plot(d.N, 100 * d.acc, ls, marker="o", ms=3, label=m)
        ax[1].plot(d.mean_token_cost, 100 * d.acc, ls, marker="o", ms=3, label=m)
    ax[0].plot(cov.N, 100 * cov.coverage, "k:", lw=2, label="oracle coverage")
    ax[1].plot(cov.mean_token_cost, 100 * cov.coverage, "k:", lw=2, label="oracle coverage")
    ax[0].set(xscale="log", xticks=[1, 2, 4, 8, 16, 32], xticklabels=[1, 2, 4, 8, 16, 32], xlabel="candidates N",
              ylabel="accuracy (%)", title=f"{mkey}: accuracy vs N (held-out, mean over 10 orders)")
    ax[1].set(xscale="log", xlabel="mean generated-token cost per question", ylabel="accuracy (%)",
              title="accuracy vs token cost (FirstAns consumes 1 rollout)")
    ax[0].legend(fontsize=6, ncol=2)
    fig.tight_layout()
    fig.savefig(os.path.join(A, "accuracy_vs_N_and_cost.png"), dpi=150)

    fig, ax = plt.subplots(1, 2, figsize=(12, 4.5))
    best = T.loc[T.groupby("N").acc.idxmax()].sort_values("N")
    ax[0].plot(cov.N, 100 * cov.coverage, "k-o", label="oracle coverage")
    ax[0].plot(cov.N, 100 * cov.acc, "-o", label="SC accuracy")
    ax[0].plot(best.N, 100 * best.acc, "-s", label="best method accuracy")
    ax[0].fill_between(cov.N, 100 * cov.acc, 100 * cov.coverage, alpha=.15, label="SC selection gap")
    ax[0].set(xscale="log", xticks=[1, 2, 4, 8, 16, 32], xticklabels=[1, 2, 4, 8, 16, 32], xlabel="N", ylabel="%",
              title="coverage vs achieved selection")
    ax[0].legend(fontsize=7)
    x = np.arange(len(cov))
    ax[1].bar(x, 100 * cov.acc, label="SC correct")
    ax[1].bar(x, 100 * cov.selection_fail, bottom=100 * cov.acc, label="correct available, missed (selection)")
    ax[1].bar(x, 100 * cov.sampling_fail, bottom=100 * (cov.acc + cov.selection_fail), label="no correct candidate (sampling)")
    ax[1].set(xticks=x, xticklabels=cov.N, xlabel="N", ylabel="% of questions", title="failure decomposition (SC)")
    ax[1].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(os.path.join(A, "coverage_gap.png"), dpi=150)

    U = pd.read_csv(os.path.join(R, "new_sample_utility.csv"))
    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    ax[0].plot(cov.N, cov.n_distinct, "o-")
    ax[0].set(xscale="log", xticks=[1, 2, 4, 8, 16, 32], xticklabels=[1, 2, 4, 8, 16, 32], xlabel="N",
              ylabel="mean # distinct answer groups", title="answer diversity")
    ax[1].plot(U.n, U.p_new_group, label="P(sample n opens a new answer group)")
    ax[1].plot(U.n, U.p_first_correct_appears, label="P(first correct candidate appears at n)")
    ax[1].set(yscale="log", xlabel="sample index n", title="marginal value of another sample (all questions)")
    ax[1].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(os.path.join(A, "diversity.png"), dpi=150)

    Bt = pd.read_csv(os.path.join(R, "comparisons_bootstrap.csv"))
    for N in [8, 32]:
        d = T[T.N == N][["method", "acc", "coverage", "mean_token_cost"]].copy()
        d["acc"] *= 100
        d["coverage"] *= 100
        with open(os.path.join(A, f"method_table_N{N}.md"), "w") as f:
            f.write(d.to_markdown(index=False, floatfmt=".2f") + "\n\nPaired bootstrap (1,000 question resamples):\n\n" +
                    Bt[Bt.N == N].to_markdown(index=False, floatfmt=".2f"))


def qualitative(cfg):
    """Selection rules (stated, fixed):
    (1) selection disagreement: first 3 held-out questions per model (split order) where, at N=32 in order 0,
        self-consistency and the model's best quality+RWC method (SelfCert+RWC for MATH, VisConf+RWC for MathVista)
        choose different answers.
    (2) revision: the first 2 live questions (split order) for each of the transitions W->C and C->W, plus the first
        C->C and W->W question.
    (3) premature stopping: first 3 held-out (question, order) incorrect early stops per adaptive family per model,
        preferring ones where fixed-32 SC would have been correct."""
    lines = ["# Task 3 qualitative cases", "", qualitative.__doc__, ""]
    for mkey in ["llm", "vlm"]:
        try:
            blob = load_pool(mkey)
        except FileNotFoundError:
            continue
        pool, sp = blob["pool"], blob["splits"]
        vision = cfg["models"][mkey]["kind"] == "mathvista"
        m2 = "VisConf+RWC" if vision else "SelfCert+RWC"
        lines.append(f"\n## ({mkey}) selection disagreement: SC vs {m2}, N=32, order 0\n")
        k = 0
        for sid in sp["eval"]:
            q = pool[sid]
            o = np.asarray(sp["orders_positions"][sid][0])
            s = all_selections(q, o, vision)
            if s["SC"] != s[m2]:
                cnt = pd.Series(q["labels"]).value_counts().head(5).to_dict()
                lines.append(f"- **{sid}** gold=`{q.get('gold_norm', q['gold'])}` | SC→`{s['SC']}` "
                             f"({'✓' if q['group_correct'][s['SC']] else '✗'}) | {m2}→`{s[m2]}` "
                             f"({'✓' if q['group_correct'][s[m2]] else '✗'}) | top groups {cnt} | "
                             f"pool correct {q['correct'].mean():.2f}")
                k += 1
            if k == 3:
                break
        for fam in ["agreement", "quality_aware"]:
            f = os.path.join(results_dir("task3", mkey, "adaptive"), "early_stop_errors.csv")
            if not os.path.exists(f) or os.path.getsize(f) < 5:
                continue
            E = pd.read_csv(f)
            E = E[E.family == fam].sort_values("fixed32_would_recover", ascending=False, kind="stable").head(3)
            lines.append(f"\n## ({mkey}) premature stops - {fam}\n")
            for _, r in E.iterrows():
                lines.append(f"- {r.sample_id} order {r.order}: stopped at n={r.stop_n} with `{r.stopped_answer}` "
                             f"(lead {r.lead_frac:.2f}, margin {r.rwc_margin:.2f}); fixed-32 → `{r.fixed32_answer}` "
                             f"(correct={r.fixed32_would_recover}); gold `{r.gold}`; pool correct {r.pool_correct_frac:.2f}")
    lp = os.path.join(results_dir("task3", "live"), "per_question.csv")
    if os.path.exists(lp):
        L = pd.read_csv(lp)
        calls = pd.DataFrame(read_jsonl(os.path.join(results_dir("task3", "live"), "calls.jsonl")))
        lines.append("\n## (live) refinement transitions\n")
        for t, n in [("W->C", 2), ("C->W", 2), ("C->C", 1), ("W->W", 1)]:
            for sid in L[L.transition == t].sample_id.head(n):
                c0 = calls[(calls.sample_id == sid) & (calls.tag == "par0")].iloc[0]
                c1 = calls[(calls.sample_id == sid) & (calls.tag == "refine")].iloc[0]
                lines.append(f"- **{t}** {sid}: initial boxed `{c0.boxed}` → revised `{c1.boxed}`; revision excerpt: "
                             f"\"{str(c1.response)[:300].replace(chr(10), ' ')}…\"")
    with open(os.path.join(results_dir("task3"), "qualitative_cases.md"), "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task3.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    cfg = load_config(a.config, a.set)
    for mk in ["llm", "vlm"]:
        if os.path.exists(os.path.join(results_dir("task3", mk), "budget_table.csv")):
            plots(mk)
    qualitative(cfg)
