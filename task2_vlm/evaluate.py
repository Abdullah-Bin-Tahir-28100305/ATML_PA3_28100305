"""Task 2 evaluation.

Development (during training): caption reference loss (96 dev captions) and reference-answer NLL (termination
excluded; token mean within example, then mean over examples) on both VQA dev suites under correct /
complementary-partner / no-image inputs, plus the image-swap penalty dNLL = NLL(y|q,I_partner) - NLL(y|q,I_correct).

Final test (after training, never used for selection): greedy, thinking disabled, 16 new tokens, batch 8, four
conditions {correct, complementary swap, mismatched (fixed mapping to an image of another pair in the same suite),
no image}; questions and ORIGINAL references are fixed across conditions.

Usage:  python -m task2_vlm.evaluate --stage_ckpt final    (final-test evaluation of the completed Stage-3 model)
"""
import argparse
import os
import time

import numpy as np
import torch

from common.utils import load_config, load_json, out_dir, results_dir, save_json, set_seed, write_jsonl
from task2_vlm.data import load_manifests
from task2_vlm.vqa_eval import exact_match, normalize_answer, soft_accuracy

CONDITIONS = ["correct", "swap", "mismatched", "no_image"]


def suite_items(pairs):
    """Flatten pairs -> items with pair index, side, partner image."""
    out = []
    for pi, p in enumerate(pairs):
        for s, it in enumerate(p["items"]):
            partner = p["items"][1 - s]
            out.append({**it, "pair_id": p["pair_id"], "pair_idx": pi, "side": s, "kind": p["kind"],
                        "task": "short", "partner_image_key": partner["image_key"], "partner_answer": partner["answer"],
                        "partner_consensus": partner.get("consensus", partner["answer"])})
    return out


def image_for(item, cond, mismatch_map):
    if cond == "correct":
        return item["image_key"]
    if cond == "swap":
        return item["partner_image_key"]
    if cond == "mismatched":
        return mismatch_map[f"{item['pair_id']}|{item['side']}"]
    return None


def mismatch_mapping(pairs, seed):
    """Fixed before generation: item (pair i, side s) -> image of side s of pair perm-successor of i (never its own
    pair). Saved and reused for every comparison."""
    rng = np.random.default_rng(seed)
    perm = rng.permutation(len(pairs))
    succ = {int(perm[k]): int(perm[(k + 1) % len(perm)]) for k in range(len(perm))}
    m = {}
    for i, p in enumerate(pairs):
        for s in range(2):
            j = succ[i]
            assert j != i
            m[f"{p['pair_id']}|{s}"] = pairs[j]["items"][s]["image_key"]
    return m


@torch.no_grad()
def nll_items(vlm, store, items, cond, mismatch_map=None, bs=16):
    vals = []
    for s in range(0, len(items), bs):
        chunk = items[s:s + bs]
        keys = [image_for(it, cond, mismatch_map) for it in chunk]
        feats = [None] * len(keys) if cond == "no_image" else list(store.get(keys))
        b = vlm.build(chunk, feats, with_target=True)
        vals.append(vlm.per_example_nll(b, include_eot=False).cpu())
    return torch.cat(vals).numpy()


@torch.no_grad()
def dev_eval(vlm, store, man, update, stage):
    vlm.eval()
    res = {"update": update, "stage": stage}
    caps = [{"task": "caption", "question": None, "answer": c["caption"], "image_key": c["image_key"]}
            for c in man["caption_dev"]]
    losses = []
    for s in range(0, len(caps), 16):
        ch = caps[s:s + 16]
        b = vlm.build(ch, list(store.get([c["image_key"] for c in ch])), with_target=True)
        losses.append(vlm.per_example_nll(b, include_eot=True).cpu())
    res["caption_dev_loss"] = float(torch.cat(losses).mean())
    for suite, key in [("grounding", "grounding_dev"), ("vqa", "vqa_dev")]:
        items = suite_items(man[key])
        nl = {c: nll_items(vlm, store, items, c) for c in ["correct", "swap", "no_image"]}
        kinds = np.array([it["kind"] for it in items])
        for c, v in nl.items():
            res[f"{suite}_nll_{c}"] = float(v.mean())
            for k in sorted(set(kinds)):
                res[f"{suite}_nll_{c}_{k}"] = float(v[kinds == k].mean())
        res[f"{suite}_swap_penalty"] = float((nl["swap"] - nl["correct"]).mean())
        res[f"{suite}_noimage_penalty"] = float((nl["no_image"] - nl["correct"]).mean())
        for k in sorted(set(kinds)):
            res[f"{suite}_swap_penalty_{k}"] = float((nl["swap"] - nl["correct"])[kinds == k].mean())
    vlm.train()
    return res


# ----------------------------------------------------------------------------- final evaluation
def score_item(it, pred, suite):
    if suite == "vqa":
        return soft_accuracy(pred, it["answers"])
    return exact_match(pred, it["answer"])


def canonical(it, suite):
    return it["consensus"] if suite == "vqa" else normalize_answer(it["answer"])


def boot_pairs(per_pair, n, seed):
    """per_pair: dict name -> array over pairs (same pair order). Returns mean + 95% CI per name, resampling
    complete pairs with the SAME sampled pair IDs across names (paired differences)."""
    rng = np.random.default_rng(seed)
    P = len(next(iter(per_pair.values())))
    idx = rng.integers(0, P, (n, P))
    out = {}
    for k, v in per_pair.items():
        v = np.asarray(v, float)
        m = v[idx].mean(1)
        out[k] = {"mean": float(v.mean()), "ci95": [float(np.quantile(m, .025)), float(np.quantile(m, .975))]}
    return out


@torch.no_grad()
def final_eval(cfg, vlm, store, man, tag="final"):
    vlm.eval()
    set_seed(cfg["seed"])
    E = cfg["final_eval"]
    od = out_dir("task2", cfg["run_name"], "final_eval")
    rd = results_dir("task2", cfg["run_name"], "final_eval")
    summary = {}
    for suite, key in [("grounding", "grounding_test"), ("vqa", "vqa_test")]:
        pairs = man[key]
        items = suite_items(pairs)
        mm_path = os.path.join(results_dir("task2", "splits"), f"mismatch_map_{key}.json")
        if os.path.exists(mm_path):
            mm = load_json(mm_path)
        else:
            mm = mismatch_mapping(pairs, cfg["seed"])
            save_json(mm, mm_path)
        rows = []
        for cond in CONDITIONS:
            preds, times = [], []
            for s in range(0, len(items), E["batch_size"]):
                ch = items[s:s + E["batch_size"]]
                keys = [image_for(it, cond, mm) for it in ch]
                feats = list(store.get(keys)) if cond != "no_image" else [None] * len(ch)
                torch.cuda.synchronize()
                t0 = time.perf_counter()
                out = vlm.generate(ch, feats, E["max_new_tokens"])
                torch.cuda.synchronize()
                times.append((time.perf_counter() - t0) / len(ch))
                preds += out
            nll = nll_items(vlm, store, items, cond, mm)
            for it, p, n in zip(items, preds, nll):
                rows.append({"suite": suite, "condition": cond, "pair_id": it["pair_id"], "pair_idx": it["pair_idx"],
                             "side": it["side"], "kind": it["kind"], "question": it["question"],
                             "image_key": image_for(it, cond, mm), "reference": it["answer"],
                             "answers": it.get("answers"), "canonical": canonical(it, suite),
                             "partner_canonical": normalize_answer(it["partner_consensus"]) if suite == "vqa"
                             else normalize_answer(it["partner_answer"]),
                             "prediction": p, "pred_norm": normalize_answer(p), "score": score_item(it, p, suite),
                             "ref_nll": float(n), "gen_sec_per_answer": float(np.mean(times))})
        write_jsonl(os.path.join(od, f"{suite}_predictions.jsonl"), rows)
        write_jsonl(os.path.join(rd, f"{suite}_predictions.jsonl"), rows)
        summary[suite] = summarize_suite(rows, suite, E["bootstrap"], cfg["seed"])
    save_json(summary, os.path.join(rd, "final_summary.json"))
    vlm.train()
    return summary


def summarize_suite(rows, suite, n_boot, seed):
    import pandas as pd
    df = pd.DataFrame(rows)
    out = {"conditions": {}}
    pair_ids = sorted(df.pair_id.unique())
    per_pair = {}
    for cond in CONDITIONS:
        d = df[df.condition == cond].sort_values(["pair_id", "side"])
        g = d.groupby("pair_id")
        pair_acc = g.apply(lambda x: float(all(x.pred_norm == x.canonical))).reindex(pair_ids)
        identical = g.apply(lambda x: float(x.pred_norm.iloc[0] == x.pred_norm.iloc[1])).reindex(pair_ids)
        mean_score = g.score.mean().reindex(pair_ids)
        mean_nll = g.ref_nll.mean().reindex(pair_ids)
        follows = (d.pred_norm == d.partner_canonical) & (d.pred_norm != d.canonical)
        stays = d.pred_norm == d.canonical
        c = {"accuracy": float(d.score.mean()), "pair_accuracy": float(pair_acc.mean()),
             "identical_answer_rate": float(identical.mean()), "empty_answer_rate": float((d.pred_norm == "").mean()),
             "mean_ref_nll": float(d.ref_nll.mean()), "gen_sec_per_answer": float(d.gen_sec_per_answer.mean()),
             "pred_equals_original_canonical_rate": float(stays.mean()),
             "pred_equals_partner_answer_rate": float(follows.mean()),
             "pred_unsupported_rate": float((~stays & ~(d.pred_norm == d.partner_canonical)).mean()),
             "by_kind": {k: {"accuracy": float(x.score.mean()), "n": int(len(x)), "mean_ref_nll": float(x.ref_nll.mean())}
                         for k, x in d.groupby("kind")}}
        out["conditions"][cond] = c
        per_pair[f"{cond}_acc"] = mean_score.values
        per_pair[f"{cond}_nll"] = mean_nll.values
        per_pair[f"{cond}_pair_acc"] = pair_acc.values
    for other in ["swap", "mismatched", "no_image"]:
        per_pair[f"adv_acc_correct_minus_{other}"] = per_pair["correct_acc"] - per_pair[f"{other}_acc"]
        per_pair[f"adv_nll_{other}_minus_correct"] = per_pair[f"{other}_nll"] - per_pair["correct_nll"]
    out["bootstrap_pairs"] = boot_pairs(per_pair, n_boot, seed)
    out["n_pairs"] = len(pair_ids)
    # per-kind correct-image advantages with pair bootstrap
    kinds = {}
    for k in sorted(df.kind.unique()):
        dk = df[df.kind == k]
        pk = sorted(dk.pair_id.unique())
        a = dk[dk.condition == "correct"].groupby("pair_id").score.mean().reindex(pk).values
        b = dk[dk.condition == "swap"].groupby("pair_id").score.mean().reindex(pk).values
        kinds[k] = boot_pairs({"adv_acc_correct_minus_swap": a - b}, n_boot, seed)
    out["by_kind_swap_advantage"] = kinds
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task2.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    cfg = load_config(a.config, a.set)
    from task2_vlm.cache_features import FeatureStore
    from task2_vlm.train import load_final_model
    vlm = load_final_model(cfg)
    s = final_eval(cfg, vlm, FeatureStore(cfg), load_manifests(cfg))
    import json
    print(json.dumps({k: {c: {m: v for m, v in d.items() if m != "by_kind"} for c, d in s[k]["conditions"].items()}
                      for k in s}, indent=1))
