"""Task 2 analysis -> results/task2/<run>/analysis/

  learning_curves.png       training loss (smoothed) + dev caption loss + answer NLL (correct/swap/no-image) per
                            suite + image-swap penalty, stage boundaries marked
  dev_metrics.csv           all development metrics by update
  final_tables.md / .csv    both suites x 4 conditions: accuracy, per-type accuracy, pair accuracy, identical-answer
                            rate, empty rate, mean reference NLL, gen time; follow-partner analysis; correct-image
                            advantages with 4,000-sample pair-bootstrap 95% CIs
  qualitative_pairs.png/.md complementary pairs with shared question, references and predictions under all 4
                            conditions, chosen by a STATED rule (see select_examples)
  resources.md              feature caching, training time per stage, peak memory, trainable params by stage,
                            generation time per answer
  compression_comparison.md (if the optional 49-token run exists)
Usage:  python -m task2_vlm.analyze [--run main196]
"""
import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common.utils import load_config, load_json, read_jsonl, results_dir
from task2_vlm.evaluate import CONDITIONS
from task2_vlm.prepare_data import droot

STAGE_BOUNDS = [2250, 3750, 4500]


def smooth(x, w=50):
    x = np.asarray(x, float)
    if len(x) < w:
        return x
    return np.concatenate([np.full(w - 1, np.nan), np.convolve(x, np.ones(w) / w, "valid")])


def learning_curves(run):
    R = results_dir("task2", run)
    A = results_dir("task2", run, "analysis")
    tl = pd.DataFrame(read_jsonl(os.path.join(R, "train_log.jsonl")))
    dv = pd.DataFrame(read_jsonl(os.path.join(R, "dev_eval.jsonl"))).sort_values("update")
    dv.to_csv(os.path.join(A, "dev_metrics.csv"), index=False, float_format="%.5g")
    fig, ax = plt.subplots(2, 3, figsize=(16, 8))
    ax = ax.ravel()
    ax[0].plot(tl["update"], tl.loss, alpha=.15)
    ax[0].plot(tl["update"], smooth(tl.loss), c="C0")
    ax[0].set(title="training loss (example-weighted; 50-update avg)", xlabel="update", ylabel="nats/token")
    ax[1].plot(dv["update"], dv.caption_dev_loss, "o-")
    ax[1].set(title="dev caption reference loss", xlabel="update")
    for j, suite in enumerate(["grounding", "vqa"]):
        a = ax[2 + j]
        for c, st in [("correct", "o-"), ("swap", "s--"), ("no_image", "^:")]:
            a.plot(dv["update"], dv[f"{suite}_nll_{c}"], st, label=c)
        a.set(title=f"{suite} dev reference-answer NLL (excl. EOT)", xlabel="update")
        a.legend()
    for suite in ["grounding", "vqa"]:
        ax[4].plot(dv["update"], dv[f"{suite}_swap_penalty"], "o-", label=f"{suite}: ΔNLL swap")
        ax[4].plot(dv["update"], dv[f"{suite}_noimage_penalty"], "x:", label=f"{suite}: ΔNLL no-image")
    ax[4].axhline(0, c="gray", lw=.5)
    ax[4].set(title="image-swap penalty ΔNLL = NLL(partner) − NLL(correct)", xlabel="update")
    ax[4].legend(fontsize=7)
    kinds = [c for c in dv.columns if c.startswith("vqa_swap_penalty_") or c.startswith("grounding_swap_penalty_")]
    for c in kinds:
        ax[5].plot(dv["update"], dv[c], "o-", label=c.replace("_swap_penalty_", ": "))
    ax[5].axhline(0, c="gray", lw=.5)
    ax[5].set(title="swap penalty by question type", xlabel="update")
    ax[5].legend(fontsize=7)
    for a in ax:
        for b in STAGE_BOUNDS[:-1]:
            a.axvline(b, c="k", ls=":", lw=.8)
    fig.suptitle("Stage 1 caption alignment | Stage 2 visual instruction | Stage 3 natural VQA (dotted = boundaries)")
    fig.tight_layout()
    fig.savefig(os.path.join(A, "learning_curves.png"), dpi=150)


def fmt_ci(d):
    return f"{100*d['mean']:.1f} [{100*d['ci95'][0]:.1f}, {100*d['ci95'][1]:.1f}]"


def final_tables(run):
    R = results_dir("task2", run, "final_eval")
    A = results_dir("task2", run, "analysis")
    S = load_json(os.path.join(R, "final_summary.json"))
    lines, rows = [], []
    for suite, s in S.items():
        lines.append(f"\n### {suite} (final test, {s['n_pairs']} pairs / {2*s['n_pairs']} questions)\n")
        lines.append("| condition | acc (95% CI) | " + " | ".join(sorted(s["conditions"]["correct"]["by_kind"])) +
                     " | pair acc (95% CI) | identical | empty | = original | = partner | unsupported | mean ref NLL | s/answer |")
        lines.append("|" + "---|" * (10 + len(s["conditions"]["correct"]["by_kind"])))
        B = s["bootstrap_pairs"]
        for c in CONDITIONS:
            d = s["conditions"][c]
            kinds = " | ".join(f"{100*d['by_kind'][k]['accuracy']:.1f}" for k in sorted(d["by_kind"]))
            lines.append(f"| {c} | {fmt_ci(B[f'{c}_acc'])} | {kinds} | {fmt_ci(B[f'{c}_pair_acc'])} | "
                         f"{100*d['identical_answer_rate']:.1f} | {100*d['empty_answer_rate']:.1f} | "
                         f"{100*d['pred_equals_original_canonical_rate']:.1f} | {100*d['pred_equals_partner_answer_rate']:.1f} | "
                         f"{100*d['pred_unsupported_rate']:.1f} | {d['mean_ref_nll']:.3f} | {d['gen_sec_per_answer']:.4f} |")
            rows.append({"suite": suite, "condition": c, **{k: v for k, v in d.items() if k != "by_kind"},
                         **{f"acc_{k}": v["accuracy"] for k, v in d["by_kind"].items()}})
        lines.append("\nCorrect-image advantages (pair bootstrap, 4,000 resamples, matched pair IDs):\n")
        for o in ["swap", "mismatched", "no_image"]:
            a, n = B[f"adv_acc_correct_minus_{o}"], B[f"adv_nll_{o}_minus_correct"]
            lines.append(f"- vs {o}: Δacc = {fmt_ci(a)} pp; ΔNLL = {n['mean']:.3f} [{n['ci95'][0]:.3f}, {n['ci95'][1]:.3f}]")
        for k, v in s["by_kind_swap_advantage"].items():
            lines.append(f"- {k}: Δacc(correct − swap) = {fmt_ci(v['adv_acc_correct_minus_swap'])} pp")
    with open(os.path.join(A, "final_tables.md"), "w") as f:
        f.write("\n".join(lines))
    pd.DataFrame(rows).to_csv(os.path.join(A, "final_tables.csv"), index=False, float_format="%.4g")
    print("\n".join(lines))


def select_examples(df, per_cat=2):
    """Stated selection rule: within each suite, sort pairs by pair_id and take the first `per_cat` pairs in each
    outcome category under the CORRECT condition: (a) both images answered correctly, (b) identical answer on both
    images (insensitive to the image), (c) at least one wrong but not identical. No other filtering."""
    c = df[df.condition == "correct"]
    out = {}
    for suite, d in c.groupby("suite"):
        cats = {"both_correct": [], "identical_answers": [], "other_errors": []}
        for pid, g in sorted(d.groupby("pair_id"), key=lambda t: t[0]):
            g = g.sort_values("side")
            ok = all(g.pred_norm == g.canonical)
            same = g.pred_norm.iloc[0] == g.pred_norm.iloc[1]
            cat = "both_correct" if ok else ("identical_answers" if same else "other_errors")
            if len(cats[cat]) < per_cat:
                cats[cat].append(pid)
        out[suite] = cats
    return out


def qualitative(cfg, run):
    from PIL import Image
    R = results_dir("task2", run, "final_eval")
    A = results_dir("task2", run, "analysis")
    df = pd.concat([pd.DataFrame(read_jsonl(os.path.join(R, f"{s}_predictions.jsonl"))) for s in ["grounding", "vqa"]])
    sel = select_examples(df)
    files = load_json(os.path.join(droot(cfg, "manifests"), "needed_images.json"))
    md = ["# Complementary-pair examples (selection rule in task2_vlm/analyze.py::select_examples)\n"]
    picks = [(s, cat, pid) for s, cats in sel.items() for cat, pids in cats.items() for pid in pids]
    fig, axes = plt.subplots(len(picks), 2, figsize=(9, 3.6 * len(picks)))
    axes = np.atleast_2d(axes)
    for r, (suite, cat, pid) in enumerate(picks):
        g = df[(df.pair_id == pid)]
        md.append(f"\n## [{suite} / {cat}] {pid}\n**Q:** {g.question.iloc[0]}\n")
        for side in [0, 1]:
            gs = g[g.side == side]
            corr = gs[gs.condition == "correct"].iloc[0]
            preds = {c: gs[gs.condition == c].prediction.iloc[0] for c in CONDITIONS}
            refs = corr.answers if isinstance(corr.answers, list) else [corr.reference]
            md.append(f"- image {side} ({corr.image_key}) ref=`{corr.reference}` | " +
                      " | ".join(f"{c}: `{p}`" for c, p in preds.items()))
            split = corr.image_key.split(":")[0]
            axes[r, side].imshow(Image.open(droot(cfg, "images", split, files[corr.image_key])).convert("RGB"))
            axes[r, side].axis("off")
            axes[r, side].set_title(f"{g.question.iloc[0][:60]}\nref: {corr.reference} | correct→{preds['correct']} | "
                                    f"swap→{preds['swap']}\nmismatch→{preds['mismatched']} | no-img→{preds['no_image']}",
                                    fontsize=7)
    fig.tight_layout()
    fig.savefig(os.path.join(A, "qualitative_pairs.png"), dpi=110)
    with open(os.path.join(A, "qualitative_pairs.md"), "w") as f:
        f.write("\n".join(md))


def resources(run):
    A = results_dir("task2", run, "analysis")
    fc = os.path.join(results_dir("task2", "resources"), "feature_caching.json")
    ti = load_json(os.path.join(results_dir("task2", run), "training_info.json"))
    S = load_json(os.path.join(results_dir("task2", run, "final_eval"), "final_summary.json"))
    lines = ["| item | value |", "|---|---|"]
    if os.path.exists(fc):
        f = load_json(fc)
        lines += [f"| feature caching: images | {f['n_images']} |", f"| feature caching: encode time (s) | {f['encode_sec']:.1f} |",
                  f"| feature caching: peak GPU MB | {f['peak_gpu_mem_mb']['allocated_mb']:.0f} |",
                  f"| feature cache size (GB) | {f['file_bytes']/2**30:.2f} |"]
    lines += [f"| training total (h) | {ti['train_hours']:.2f} |"]
    lines += [f"| stage {k} time (h) | {v/3600:.2f} |" for k, v in ti["stage_sec"].items()]
    lines += [f"| trainable params, {k} | {v:,} |" for k, v in ti["trainable_by_stage"].items()]
    lines += [f"| training peak GPU MB (allocated) | {ti['peak_gpu_mem_mb_this_session']['allocated_mb']:.0f} |",
              f"| skipped (overflow) updates | {len(ti['skipped_updates'])} |", f"| GPU | {ti['gpu']} |"]
    for suite, s in S.items():
        for c in CONDITIONS:
            lines.append(f"| gen s/answer {suite}/{c} | {s['conditions'][c]['gen_sec_per_answer']:.4f} |")
    lines.append(f"\nMixture draw counts: {ti['mixture_counts']}")
    with open(os.path.join(A, "resources.md"), "w") as f:
        f.write("\n".join(lines))


def compression(main="main196", alt="pool49"):
    pa = os.path.join(results_dir("task2", alt, "final_eval"), "final_summary.json")
    if not os.path.exists(pa):
        return
    lines = ["| run | suite | acc correct | acc swap | Δacc | pair acc | train h | peak MB | s/answer |", "|" + "---|" * 9]
    for run in [main, alt]:
        S = load_json(os.path.join(results_dir("task2", run, "final_eval"), "final_summary.json"))
        ti = load_json(os.path.join(results_dir("task2", run), "training_info.json"))
        for suite, s in S.items():
            c = s["conditions"]
            lines.append(f"| {run} | {suite} | {100*c['correct']['accuracy']:.1f} | {100*c['swap']['accuracy']:.1f} | "
                         f"{fmt_ci(s['bootstrap_pairs']['adv_acc_correct_minus_swap'])} | {100*c['correct']['pair_accuracy']:.1f} | "
                         f"{ti['train_hours']:.2f} | {ti['peak_gpu_mem_mb_this_session']['allocated_mb']:.0f} | "
                         f"{c['correct']['gen_sec_per_answer']:.4f} |")
    with open(os.path.join(results_dir("task2"), "compression_comparison.md"), "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task2.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    cfg = load_config(a.config, a.set)
    run = cfg["run_name"]
    learning_curves(run)
    final_tables(run)
    qualitative(cfg, run)
    resources(run)
    compression()
