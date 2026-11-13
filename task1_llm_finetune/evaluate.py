"""Task 1 evaluation with lm-evaluation-harness (fixed task definitions, complete splits, zero-shot, FP16).

* GSM8K: gsm8k_cot, num_fewshot=0, non-thinking chat template, greedy, max_gen_toks=1024, stock stop rules and
  filters; primary metric = exact_match under the 'flexible-extract' filter ('[invalid]' -> incorrect).
* HellaSwag / BoolQ / ARC-Easy: stock completion prompts, no chat template, candidate log-likelihood.
* Adapters are merged into a FRESH FP16 backbone copy (projected factors for OPLoRA) and a merged-vs-unmerged
  logit check is run on a small batch before evaluation.
* Saves aggregate scores, raw per-example samples, and a per-example table with extracted GSM8K answers,
  extractability and truncation flags.

Usage:  python -m task1_llm_finetune.evaluate --condition lora16
"""
import argparse
import json
import os
import shutil

import torch

from common.lora import attach_unmerged_factors, merge_factors_into_model
from common.utils import load_config, out_dir, results_dir, save_json, set_seed, write_jsonl
from task1_llm_finetune.data import make_nothink_tokenizer, nothink_tokenizer_dir
from task1_llm_finetune.train import export_dir


def merged_dir(cond):
    return os.path.join(out_dir("task1", "merged"), cond)


@torch.no_grad()
def merged_vs_unmerged_check(cfg, sd, merged_model, tok):
    from transformers import AutoModelForCausalLM
    texts = ["Natalia sold clips to 48 of her friends in April. How many clips did she sell?",
             "The capital of France is", "Write a haiku about the ocean."]
    enc = tok(texts, return_tensors="pt", padding=True).to("cuda")
    unmerged = AutoModelForCausalLM.from_pretrained(cfg["model_id"], revision=cfg.get("model_revision"),
                                                    torch_dtype=torch.float16).cuda().eval()
    attach_unmerged_factors(unmerged, sd)
    unmerged.eval()
    a = unmerged(**enc).logits.float()
    b = merged_model(**enc).logits.float()
    m = enc["attention_mask"].bool()
    diff = (a - b).abs()[m]
    res = {"max_abs_logit_diff": float(diff.max()), "mean_abs_logit_diff": float(diff.mean()),
           "argmax_agreement": float((a.argmax(-1)[m] == b.argmax(-1)[m]).float().mean()),
           "note": "FP16 merged vs FP16 backbone + FP32 unmerged adapters; small diffs come from FP16 rounding"}
    del unmerged
    torch.cuda.empty_cache()
    return res


def prepare_model(cfg, cond):
    """Returns a local/hub path usable by lm-eval's HFLM."""
    variant, update = cfg["eval"]["conditions"][cond]
    if variant is None:
        return cfg["model_id"]
    vcfg = cfg["variants"][variant]
    ed = export_dir(variant, update)
    if vcfg["kind"] == "full":
        return ed
    md = merged_dir(cond)
    if os.path.exists(os.path.join(md, "merge_check.json")):
        return md
    from safetensors.torch import load_file
    from transformers import AutoModelForCausalLM, AutoTokenizer
    sd = load_file(os.path.join(ed, "adapter_effective_factors.safetensors"))
    model = AutoModelForCausalLM.from_pretrained(cfg["model_id"], revision=cfg.get("model_revision"),
                                                 torch_dtype=torch.float16).cuda().eval()
    merge_factors_into_model(model, sd)
    tok = AutoTokenizer.from_pretrained(nothink_tokenizer_dir(cfg))
    tok.padding_side = "right"
    check = merged_vs_unmerged_check(cfg, sd, model, tok)
    model.save_pretrained(md, safe_serialization=True)
    tok.save_pretrained(md)
    save_json(check, os.path.join(md, "merge_check.json"))
    save_json(check, os.path.join(results_dir("task1", "eval", cond), "merge_check.json"))
    print("merge check:", check)
    del model
    torch.cuda.empty_cache()
    return md


def run_lm_eval(cfg, path, tasks, chat, batch_size, gen_kwargs=None):
    import lm_eval
    model_args = f"pretrained={path},tokenizer={nothink_tokenizer_dir(cfg)},dtype=float16"
    kw = dict(model="hf", model_args=model_args, tasks=tasks, num_fewshot=0, batch_size=batch_size,
              log_samples=True, apply_chat_template=chat, fewshot_as_multiturn=False, limit=cfg["eval"]["limit"],
              random_seed=cfg["seed"], numpy_random_seed=cfg["seed"], torch_random_seed=cfg["seed"],
              fewshot_random_seed=cfg["seed"])
    if gen_kwargs:
        kw["gen_kwargs"] = gen_kwargs
    return lm_eval.simple_evaluate(**kw)


def _first(x):
    while isinstance(x, (list, tuple)) and len(x):
        x = x[0]
    return x


def gsm8k_rows(samples, tok, cap):
    """Per-example table. lm-eval stores one sample per doc with filtered responses for each filter."""
    rows = {}
    for s in samples:
        did = s["doc_id"]
        r = rows.setdefault(did, {"doc_id": did, "question": s["doc"]["question"], "target": s["target"]})
        raw = _first(s["resps"])
        r["response"] = raw
        n_tok = len(tok(raw, add_special_tokens=False).input_ids) if raw else 0
        r["response_tokens"] = n_tok
        # lm-eval stops on stop strings; a response that reaches the cap was cut by max_gen_toks. Re-tokenizing
        # can differ by a couple of tokens, so we flag >= cap-2 and keep the exact count for inspection.
        r["truncated"] = n_tok >= cap - 2
        filt = s.get("filter", "none")
        r[f"extracted_{filt}"] = _first(s["filtered_resps"])
        r[f"exact_match_{filt}"] = s.get("exact_match")
    for r in rows.values():
        ex = r.get("extracted_flexible-extract")
        r["extractable"] = ex not in (None, "", "[invalid]")
    return [rows[k] for k in sorted(rows)]


def ll_rows(samples, task):
    rows = []
    for s in samples:
        rows.append({"doc_id": s["doc_id"], "target": s["target"], "acc": s.get("acc"),
                     "acc_norm": s.get("acc_norm"),
                     "pred": int(torch.tensor([float(_first(x)) for x in s["filtered_resps"]]).argmax())
                     if s.get("filtered_resps") else None})
    return sorted(rows, key=lambda r: r["doc_id"])


def evaluate(cfg, cond):
    set_seed(cfg["seed"])
    if not os.path.exists(nothink_tokenizer_dir(cfg)):
        make_nothink_tokenizer(cfg)
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(nothink_tokenizer_dir(cfg))
    path = prepare_model(cfg, cond)
    ed = out_dir("task1", "eval", cond)
    rd = results_dir("task1", "eval", cond)
    summary = {"condition": cond, "model_path": path}
    E = cfg["eval"]

    # ---- GSM8K (generation, chat template, greedy, 1,024-token cap)
    if not os.path.exists(os.path.join(ed, "gsm8k_rows.jsonl")):
        res = run_lm_eval(cfg, path, [E["gen_task"]], True, E["gen_batch_size"],
                          gen_kwargs=f"max_gen_toks={E['max_gen_toks']},do_sample=False")
        r = res["results"][E["gen_task"]]
        write_jsonl(os.path.join(ed, "gsm8k_samples_raw.jsonl"), res["samples"][E["gen_task"]])
        rows = gsm8k_rows(res["samples"][E["gen_task"]], tok, E["max_gen_toks"])
        write_jsonl(os.path.join(ed, "gsm8k_rows.jsonl"), rows)
        save_json({"results": r, "config": res.get("configs", {}).get(E["gen_task"]),
                   "versions": res.get("versions")}, os.path.join(ed, "gsm8k_lmeval.json"))
    rows = [json.loads(l) for l in open(os.path.join(ed, "gsm8k_rows.jsonl"))]
    import numpy as np
    em = np.array([float(r.get("exact_match_flexible-extract") or 0) for r in rows])
    ems = np.array([float(r.get("exact_match_strict-match") or 0) for r in rows])
    tr = np.array([r["truncated"] for r in rows])
    ext = np.array([r["extractable"] for r in rows])
    summary["gsm8k"] = {"flexible_em": float(em.mean()), "strict_em": float(ems.mean()), "n": len(rows),
                        "truncation_rate": float(tr.mean()), "extraction_rate": float(ext.mean()),
                        "em_among_extractable_nontruncated": float(em[ext & ~tr].mean()) if (ext & ~tr).any() else None,
                        "mean_response_tokens": float(np.mean([r["response_tokens"] for r in rows]))}

    # ---- likelihood tasks (no chat template)
    for task in E["ll_tasks"]:
        fn = os.path.join(ed, f"{task}_rows.jsonl")
        if not os.path.exists(fn):
            res = run_lm_eval(cfg, path, [task], False, E["ll_batch_size"])
            write_jsonl(fn, ll_rows(res["samples"][task], task))
            save_json({"results": res["results"][task], "versions": res.get("versions")},
                      os.path.join(ed, f"{task}_lmeval.json"))
        lr = json.load(open(os.path.join(ed, f"{task}_lmeval.json")))["results"]
        summary[task] = {"acc": lr.get("acc,none"), "acc_stderr": lr.get("acc_stderr,none"),
                         "acc_norm": lr.get("acc_norm,none"), "acc_norm_stderr": lr.get("acc_norm_stderr,none")}
    save_json(summary, os.path.join(ed, "summary.json"))
    save_json(summary, os.path.join(rd, "summary.json"))
    for fn in os.listdir(ed):  # per-example tables are small enough to commit (raw samples stay on Drive)
        if fn.endswith("_rows.jsonl") or fn.endswith("_lmeval.json"):
            shutil.copy(os.path.join(ed, fn), os.path.join(rd, fn))
    print(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task1.yaml")
    ap.add_argument("--condition", required=True)
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    evaluate(load_config(a.config, a.set), a.condition)
