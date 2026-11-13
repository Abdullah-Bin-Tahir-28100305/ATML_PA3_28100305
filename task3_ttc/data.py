"""Load + join the supplied rollout exports, verify token alignment, form answer groups / correctness labels,
and build the dev/eval split, live-generation subset and the 10 seeded rollout orders.

Split: sort the 1,000 question IDs lexicographically, permute with np.random.default_rng(6304); first 200 = dev,
remaining 800 = held-out eval; first 50 eval MATH questions = live-generation subset. Orders: for each question
(in sorted-ID order) draw ten permutations of its 32 sorted rollout IDs from a fresh default_rng(6304).

Usage:  python -m task3_ttc.data          (writes $PA3_OUT/task3/pool_<model>.pkl and results/task3/splits)
"""
import argparse
import os
import pickle

import numpy as np
import pandas as pd

from common.utils import REPO_ROOT, load_config, out_dir, results_dir, save_json
from task3_ttc import answers as A

KEYS = ["dataset", "sample_id", "rollout_id"]


def resolve(df, cfg, field, required=True):
    for c in cfg["columns"][field]:
        if c in df.columns:
            return c
    if required:
        raise KeyError(f"cannot resolve column '{field}' (candidates {cfg['columns'][field]}); "
                       f"available: {list(df.columns)} -> edit configs/task3.yaml:columns")
    return None


def data_path(cfg, *p):
    root = cfg["data_root"]
    if not os.path.isabs(root):
        root = os.path.join(REPO_ROOT, root)
    return os.path.join(root, *p)


def load_tables(cfg, mkey):
    m = cfg["models"][mkey]
    gen = pd.read_parquet(data_path(cfg, m["folder"], "generations.parquet"))
    prob = pd.read_parquet(data_path(cfg, m["folder"], "probability_metrics.parquet"))
    print(f"[{mkey}] generations columns: {list(gen.columns)}\n[{mkey}] probability columns: {list(prob.columns)}")
    keys = [resolve(gen, cfg, k) for k in KEYS]
    g = pd.DataFrame({"dataset": gen[keys[0]], "sample_id": gen[keys[1]].astype(str), "rollout_id": gen[keys[2]]})
    for f in ["text", "token_ids", "extracted", "gold"]:
        g[f] = gen[resolve(gen, cfg, f)]
    nt = resolve(gen, cfg, "n_tokens", required=False)
    g["n_tokens_col"] = gen[nt] if nt else np.nan
    cp = resolve(gen, cfg, "capped", required=False)
    g["capped"] = gen[cp].astype(bool) if cp else False
    for f in ["problem", "question_type", "answer_type", "precision", "choices"]:
        c = resolve(gen, cfg, f, required=False)
        if c:
            g[f] = gen[c]
    pk = [resolve(prob, cfg, k) for k in KEYS]
    p = pd.DataFrame({"dataset": prob[pk[0]], "sample_id": prob[pk[1]].astype(str), "rollout_id": prob[pk[2]],
                      "logp": prob[resolve(prob, cfg, "logp")], "kl_u_p": prob[resolve(prob, cfg, "kl_u_p")]})
    df = g.merge(p, on=KEYS, how="inner", validate="one_to_one")
    assert len(df) == len(gen) == len(prob), (len(df), len(gen), len(prob))
    if m["kind"] == "mathvista":
        att = pd.read_parquet(data_path(cfg, m["folder"], "attention_metrics.parquet"))
        print(f"[{mkey}] attention columns: {list(att.columns)}")
        ak = [resolve(att, cfg, k) for k in KEYS]
        a = pd.DataFrame({"dataset": att[ak[0]], "sample_id": att[ak[1]].astype(str), "rollout_id": att[ak[2]]})
        for f in ["attn_image", "attn_prompt", "attn_generated"]:
            a[f] = att[resolve(att, cfg, f)]
        df = df.merge(a, on=KEYS, how="inner", validate="one_to_one")
        assert len(df) == len(gen)
    return df


def mathvista_meta(cfg, df):
    need = {"question_type", "answer_type", "precision", "choices"}
    if need.issubset(df.columns):
        return {sid: {k: r[k] for k in need} for sid, r in df.groupby("sample_id").first().iterrows()}
    from datasets import load_dataset
    ds = load_dataset("AI4Math/MathVista", split="testmini")
    meta = {str(r["pid"]): {"question_type": r["question_type"], "answer_type": r["answer_type"],
                            "precision": r["precision"], "choices": r["choices"]} for r in ds}
    missing = set(df.sample_id) - set(meta)
    assert not missing, f"{len(missing)} sample_ids not found as MathVista pids, e.g. {list(missing)[:5]}"
    return meta


def build_pool(cfg, mkey):
    m = cfg["models"][mkey]
    df = load_tables(cfg, mkey)
    align = {"n_rollouts": len(df), "token_ids_vs_logp_mismatch": 0, "logp_vs_kl_mismatch": 0,
             "attn_mismatch": 0, "ncol_vs_logp_mismatch": 0, "empty_rollouts": 0}
    meta = mathvista_meta(cfg, df) if m["kind"] == "mathvista" else None
    pool = {}
    for sid, g in df.groupby("sample_id", sort=True):
        g = g.sort_values("rollout_id")
        assert len(g) == 32, (sid, len(g))
        q = {"sample_id": sid, "rollout_ids": g.rollout_id.tolist(), "gold": g.gold.iloc[0],
             "extracted": [None if A.is_missing(x) else str(x) for x in g.extracted],
             "texts": g.text.tolist(), "capped": g.capped.to_numpy(bool)}
        if "problem" in g:
            q["problem"] = g.problem.iloc[0]
        lp = [np.asarray(x, dtype=np.float64) for x in g.logp]
        kl = [np.asarray(x, dtype=np.float64) for x in g.kl_u_p]
        tid = [len(x) for x in g.token_ids]
        for a, b, c, nc in zip(lp, kl, tid, g.n_tokens_col):
            align["token_ids_vs_logp_mismatch"] += int(c != len(a))
            align["logp_vs_kl_mismatch"] += int(len(a) != len(b))
            align["ncol_vs_logp_mismatch"] += int(nc is not None and nc == nc and int(nc) != len(a))
            align["empty_rollouts"] += int(len(a) == 0)
        q["T"] = np.array([len(a) for a in lp])  # measured generated tokens (termination excluded)
        # cost proxy = retained token counts (token_ids list length, i.e. what the export retained)
        q["cost"] = np.array([n if (n == n and n is not None) else t for n, t in zip(g.n_tokens_col, tid)], dtype=float)
        q["L"] = np.array([a.mean() if len(a) else -np.inf for a in lp])
        q["K"] = np.array([b.mean() if len(b) else -np.inf for b in kl])
        q["kl"] = kl
        if m["kind"] == "mathvista":
            v = []
            for ai, ap, ag, a in zip(g.attn_image, g.attn_prompt, g.attn_generated, lp):
                ai, ap, ag = (np.asarray(x, dtype=np.float64) for x in (ai, ap, ag))
                align["attn_mismatch"] += int(not (len(ai) == len(ap) == len(ag) == len(a)))
                v.append(ai / np.maximum(ai + ap + ag, 1e-30))
            q["v"] = v
            q["V"] = np.array([x.mean() if len(x) else -np.inf for x in v])
            q["meta"] = meta[sid]
            q["labels"], _ = A.group_mathvista(q["extracted"], q["meta"])
            gn = A.mathvista_gold(q["gold"], q["meta"])
            q["gold_norm"] = gn
            q["group_correct"] = {l: A.grade_mathvista(l, gn) for l in set(q["labels"])}
        else:
            q["labels"], reps = A.group_math(q["extracted"])
            q["group_correct"] = {l: A.grade_math(l, str(q["gold"])) for l in set(q["labels"])}
        q["correct"] = np.array([q["group_correct"][l] for l in q["labels"]])
        pool[sid] = q
    try:
        import importlib.metadata as _im
        mv = _im.version("math-verify")
    except Exception:
        mv = None
    align.update({"n_capped_rollouts": int(sum(q["capped"].sum() for q in pool.values())),
                  "n_null_extracted": int(sum(sum(x is None for x in q["extracted"]) for q in pool.values())),
                  "total_retained_tokens": int(sum(q["cost"].sum() for q in pool.values())),
                  "n_questions": len(pool), "scorer_version": A.SCORER_VERSION, "math_verify_version": mv,
                  "mean_correct_rate": float(np.mean([q["correct"].mean() for q in pool.values()])),
                  "no_answer_rate": float(np.mean([np.mean([l == A.NO_ANSWER for l in q["labels"]]) for q in pool.values()]))})
    print(align)
    save_json(align, os.path.join(results_dir("task3", "checks"), f"{mkey}_alignment_and_scorer.json"))
    return pool


def make_splits(cfg, pool, mkey):
    ids = sorted(pool.keys())
    perm = np.random.default_rng(cfg["seed"]).permutation(len(ids))
    order = [ids[i] for i in perm]
    dev, ev = order[: cfg["n_dev"]], order[cfg["n_dev"]:]
    rng = np.random.default_rng(cfg["seed"])
    orders = {sid: [rng.permutation(32).tolist() for _ in range(cfg["n_orders"])] for sid in ids}  # positions
    sp = {"dev": dev, "eval": ev, "orders_positions": orders}
    if cfg["models"][mkey]["kind"] == "math":
        sp["live50"] = ev[: cfg["live"]["n_questions"]]
    save_json({k: v for k, v in sp.items() if k != "orders_positions"},
              os.path.join(results_dir("task3", "splits"), f"{mkey}_splits.json"))
    save_json({sid: [[pool[sid]["rollout_ids"][i] for i in o] for o in os_] for sid, os_ in orders.items()},
              os.path.join(results_dir("task3", "splits"), f"{mkey}_rollout_orders.json"), indent=None)
    return sp


def pool_path(mkey):
    return os.path.join(out_dir("task3"), f"pool_{mkey}.pkl")


def load_pool(mkey):
    with open(pool_path(mkey), "rb") as f:
        return pickle.load(f)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task3.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    ap.add_argument("--models", nargs="*", default=["llm", "vlm"])
    a = ap.parse_args()
    cfg = load_config(a.config, a.set)
    for mk in a.models:
        pool = build_pool(cfg, mk)
        sp = make_splits(cfg, pool, mk)
        with open(pool_path(mk), "wb") as f:
            pickle.dump({"pool": pool, "splits": sp}, f)
        print(f"[{mk}] saved", pool_path(mk))
