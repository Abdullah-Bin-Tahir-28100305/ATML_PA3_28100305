"""Task 3 Step 4: parallel sampling vs one longer response vs sequential refinement on the fixed 50-question MATH
subset (first 50 held-out questions in the permutation). Same 3,072-token total OUTPUT allowance per question:

  long      : one response, cap 3,072
  parallel  : three independent responses, cap 1,024 each; self-consistency (math-verify groups) picks the answer
  refine    : initial = parallel response #1 (shared, charged to both); then one critique-and-revision response,
              cap 2,048, conditioned on [system, user(problem+instruction), assistant(initial), user(review instr.)];
              the revised answer is returned (never chosen with gold answers)

FP16, no quantization, batch size 1, temperature 0.7 / top-p 0.8 / top-k 20 / repetition penalty 1.0, native chat
template, fixed system message. Each call is independently seeded. Every call (prompt, response, seed, input/output
tokens, truncation, extraction, wall time) is appended to a JSONL so the run can resume after a disconnect.

Usage:  python -m task3_ttc.live_generation [--analyze_only]
"""
import argparse
import hashlib
import os
import time

import numpy as np
import pandas as pd
import torch

from common.utils import (append_jsonl, load_config, out_dir, peak_memory_mb, read_jsonl, reset_peak_memory,
                          results_dir, save_json)
from task3_ttc import answers as A
from task3_ttc.data import load_pool


def call_seed(base, sid, tag):
    return int(hashlib.sha256(f"{base}|{sid}|{tag}".encode()).hexdigest()[:8], 16)


def load_model(L):
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(L["model_id"], revision=L["revision"])
    model = AutoModelForCausalLM.from_pretrained(L["model_id"], revision=L["revision"], torch_dtype=torch.float16,
                                                 attn_implementation="sdpa").cuda().eval()
    return tok, model


@torch.no_grad()
def generate(tok, model, messages, cap, seed, L):
    text = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    ids = tok(text, return_tensors="pt").to("cuda")
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    out = model.generate(**ids, do_sample=True, temperature=L["temperature"], top_p=L["top_p"], top_k=L["top_k"],
                         repetition_penalty=L["repetition_penalty"], max_new_tokens=cap,
                         pad_token_id=tok.pad_token_id or tok.eos_token_id)
    torch.cuda.synchronize()
    dt = time.perf_counter() - t0
    new = out[0, ids.input_ids.shape[1]:].tolist()
    eos = set([tok.eos_token_id] + ([tok.convert_tokens_to_ids("<|im_end|>")]))
    ended = len(new) > 0 and new[-1] in eos
    n_out = len(new) - (1 if ended else 0)
    resp = tok.decode(new, skip_special_tokens=True)
    return {"prompt": text, "response": resp, "input_tokens": int(ids.input_ids.shape[1]), "output_tokens": len(new),
            "output_tokens_excl_eos": n_out, "truncated": (not ended) and len(new) >= cap, "seconds": dt, "seed": seed,
            "cap": cap, "boxed": A.last_boxed(resp)}


def run(cfg):
    L = cfg["live"]
    assert L["revision"], "set live.revision in configs/task3.yaml to the revision listed in the supplied README.MD"
    blob = load_pool("llm")
    pool, sp = blob["pool"], blob["splits"]
    qids = sp["live50"]
    path = os.path.join(out_dir("task3", "live"), "calls.jsonl")
    done = {(r["sample_id"], r["tag"]) for r in read_jsonl(path)}
    tok, model = load_model(L)
    reset_peak_memory()
    for qi, sid in enumerate(qids):
        problem = pool[sid].get("problem")
        assert problem, "problem text not found in generations.parquet - set configs/task3.yaml:columns.problem"
        base = [{"role": "system", "content": L["system"]}, {"role": "user", "content": f"{problem}{L['prompt_join']}{L['instruction']}"}]
        jobs = [("long", base, L["long_cap"])] + [(f"par{k}", base, L["parallel_cap"]) for k in range(L["n_parallel"])]
        for tag, msgs, cap in jobs:
            if (sid, tag) in done:
                continue
            r = generate(tok, model, msgs, cap, call_seed(cfg["seed"], sid, tag), L)
            append_jsonl(path, {"sample_id": sid, "tag": tag, "messages": msgs, **r})
        if (sid, "refine") not in done:
            par0 = [r for r in read_jsonl(path) if r["sample_id"] == sid and r["tag"] == "par0"][0]
            msgs = base + [{"role": "assistant", "content": par0["response"]},
                           {"role": "user", "content": L["refine_instruction"]}]
            r = generate(tok, model, msgs, L["refine_cap"], call_seed(cfg["seed"], sid, "refine"), L)
            append_jsonl(path, {"sample_id": sid, "tag": "refine", "messages": msgs, **r})
        print(f"{qi+1}/{len(qids)} done")
    save_json({"peak_gpu_mem_mb": peak_memory_mb(), "gpu": torch.cuda.get_device_name(0),
               "model": L["model_id"], "revision": L["revision"], "dtype": "float16", "batch_size": 1},
              os.path.join(results_dir("task3", "live"), "live_env.json"))


def analyze(cfg):
    blob = load_pool("llm")
    pool, sp = blob["pool"], blob["splits"]
    calls = pd.DataFrame(read_jsonl(os.path.join(out_dir("task3", "live"), "calls.jsonl")))
    rd = results_dir("task3", "live")
    calls.drop(columns=["messages"]).to_json(os.path.join(rd, "calls.jsonl"), orient="records", lines=True)
    rows = []
    for sid in sp["live50"]:
        gold = str(pool[sid]["gold"])
        c = {t: calls[(calls.sample_id == sid) & (calls.tag == t)].iloc[0] for t in
             ["long", "par0", "par1", "par2", "refine"]}
        grade = lambda b: (not A.is_missing(b)) and A.grade_math(str(b).strip(), gold)
        par = [c[f"par{k}"] for k in range(3)]
        labels, _ = A.group_math([p["boxed"] for p in par])
        from task3_ttc.scoring import vote
        win = vote(labels)[0]
        par_ok = win != A.NO_ANSWER and A.grade_math(win, gold)
        init_ok, rev_ok = grade(c["par0"]["boxed"]), grade(c["refine"]["boxed"])
        rows.append({
            "sample_id": sid,
            "long_correct": grade(c["long"]["boxed"]), "parallel_correct": par_ok, "refine_correct": rev_ok,
            "initial_correct": init_ok,
            "long_in": c["long"]["input_tokens"], "long_out": c["long"]["output_tokens"], "long_sec": c["long"]["seconds"],
            "parallel_in": sum(p["input_tokens"] for p in par), "parallel_out": sum(p["output_tokens"] for p in par),
            "parallel_sec": sum(p["seconds"] for p in par),
            "refine_in": c["par0"]["input_tokens"] + c["refine"]["input_tokens"],
            "refine_out": c["par0"]["output_tokens"] + c["refine"]["output_tokens"],
            "refine_sec": c["par0"]["seconds"] + c["refine"]["seconds"],
            "long_trunc": c["long"]["truncated"], "parallel_trunc_any": any(p["truncated"] for p in par),
            "parallel_trunc_rate": np.mean([p["truncated"] for p in par]),
            "init_trunc": c["par0"]["truncated"], "refine_trunc": c["refine"]["truncated"],
            "long_extracted": not A.is_missing(c["long"]["boxed"]),
            "parallel_extracted_rate": np.mean([not A.is_missing(p["boxed"]) for p in par]),
            "parallel_answer_extracted": win != A.NO_ANSWER,
            "init_extracted": not A.is_missing(c["par0"]["boxed"]),
            "refine_extracted": not A.is_missing(c["refine"]["boxed"]),
            "revision_changed_answer": A.group_math([c["par0"]["boxed"], c["refine"]["boxed"]])[0][0] !=
                                       A.group_math([c["par0"]["boxed"], c["refine"]["boxed"]])[0][1],
            "transition": f"{'C' if init_ok else 'W'}->{'C' if rev_ok else 'W'}"})
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(rd, "per_question.csv"), index=False)
    summ = {}
    for s in ["long", "parallel", "refine"]:
        summ[s] = {"accuracy": df[f"{s}_correct"].mean(), "mean_input_tokens": df[f"{s}_in"].mean(),
                   "mean_output_tokens": df[f"{s}_out"].mean(), "mean_total_tokens": (df[f"{s}_in"] + df[f"{s}_out"]).mean(),
                   "mean_seconds": df[f"{s}_sec"].mean(), "total_seconds": df[f"{s}_sec"].sum()}
    summ["long"].update({"truncation_rate": df.long_trunc.mean(), "extraction_rate": df.long_extracted.mean()})
    summ["parallel"].update({"truncation_rate_per_response": df.parallel_trunc_rate.mean(),
                             "any_truncated_rate": df.parallel_trunc_any.mean(),
                             "extraction_rate_per_response": df.parallel_extracted_rate.mean(),
                             "final_answer_extracted_rate": df.parallel_answer_extracted.mean()})
    summ["refine"].update({"initial_accuracy": df.initial_correct.mean(), "initial_truncation_rate": df.init_trunc.mean(),
                           "revision_truncation_rate": df.refine_trunc.mean(),
                           "initial_extraction_rate": df.init_extracted.mean(),
                           "revision_extraction_rate": df.refine_extracted.mean(),
                           "answer_changed_rate": df.revision_changed_answer.mean(),
                           "transitions": df.transition.value_counts().to_dict()})
    rng = np.random.default_rng(cfg["seed"])
    idx = rng.integers(0, len(df), (cfg["bootstrap"], len(df)))
    for a, b in [("long", "parallel"), ("long", "refine"), ("parallel", "refine"), ("initial", "refine")]:
        d = (df[f"{b}_correct"].astype(float) - df[f"{a}_correct"].astype(float)).values
        bm = d[idx].mean(1)
        summ[f"diff_{b}_minus_{a}_pp"] = {"mean": 100 * d.mean(), "ci95": [100 * np.quantile(bm, .025),
                                                                          100 * np.quantile(bm, .975)]}
    save_json(summ, os.path.join(rd, "live_summary.json"))
    print(summ)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task3.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    ap.add_argument("--analyze_only", action="store_true")
    a = ap.parse_args()
    cfg = load_config(a.config, a.set)
    if not a.analyze_only:
        run(cfg)
    analyze(cfg)
