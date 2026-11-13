"""Task 1 qualitative behaviour: 24 prompts (8 questions x {plain, json, template}) x 7 conditions = 168 greedy
responses (thinking disabled, 512-token cap). Builds the fixed 42-response annotation subset (first and last
underlying question in the file's plain-question order, all 3 formats, all 7 conditions) with automatic helper
flags; the actual annotation (format compliance, coherence/relevance, ...) is done by you in the CSV.

The prompts file is the supplied `first_plot_questions.yaml`. Entries are grouped by stripping a trailing
`_json` / `_template` from their `id`; an entry's optional `system` field is used as the system message and the
first paraphrase is the user message.

Usage:  python -m task1_llm_finetune.qualitative            (generate all + build annotation sheet)
"""
import argparse
import csv
import json
import os
import re

import torch
import yaml

from common.utils import load_config, out_dir, read_jsonl, results_dir, write_jsonl
from task1_llm_finetune.data import nothink_tokenizer_dir
from task1_llm_finetune.evaluate import prepare_model

FORMATS = ["plain", "json", "template"]


def load_prompts(path):
    with open(path) as f:
        items = yaml.safe_load(f)
    if isinstance(items, dict):
        items = items.get("questions", list(items.values()))
    prompts, plain_order = [], []
    for it in items:
        qid = it["id"]
        fmt, base = "plain", qid
        for suf in ["_json", "_template"]:
            if qid.endswith(suf):
                fmt, base = suf[1:], qid[: -len(suf)]
        para = it.get("paraphrases") or [it.get("question") or it.get("prompt")]
        prompts.append({"qid": qid, "base": base, "format": fmt, "system": it.get("system"), "user": para[0]})
        if fmt == "plain":
            plain_order.append(base)
    assert len(prompts) == 24, f"expected 24 prompts, got {len(prompts)}"
    return prompts, plain_order


@torch.no_grad()
def generate_condition(cfg, cond, prompts):
    from transformers import AutoModelForCausalLM, AutoTokenizer
    path = prepare_model(cfg, cond)
    tok = AutoTokenizer.from_pretrained(nothink_tokenizer_dir(cfg))
    model = AutoModelForCausalLM.from_pretrained(path, torch_dtype=torch.float16).cuda().eval()
    cap = cfg["qualitative"]["max_new_tokens"]
    im_end = tok.convert_tokens_to_ids("<|im_end|>")
    out = []
    for p in prompts:
        msgs = ([{"role": "system", "content": p["system"]}] if p["system"] else []) + \
               [{"role": "user", "content": p["user"]}]
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        ids = tok(text, return_tensors="pt", add_special_tokens=False).to("cuda")
        g = model.generate(**ids, max_new_tokens=cap, do_sample=False, temperature=None, top_p=None, top_k=None,
                           eos_token_id=[im_end, tok.eos_token_id], pad_token_id=tok.pad_token_id)
        new = g[0, ids.input_ids.shape[1]:].tolist()
        ended = len(new) > 0 and new[-1] in (im_end, tok.eos_token_id)
        resp = tok.decode(new, skip_special_tokens=True)
        out.append({"condition": cond, **p, "response": resp, "n_new_tokens": len(new),
                    "truncated": (not ended) and len(new) >= cap})
    del model
    torch.cuda.empty_cache()
    return out


# ---------------------------------------------------------------- automatic helper flags (annotation aids only)
MATH_PAT = re.compile(r"(\\boxed|\\frac|\$[^$]+\$|\b\d+\s*[\+\-\*/×=]\s*\d+|step\s*\d|let'?s (solve|calculate)|"
                      r"therefore|the answer is|equation)", re.I)


def repetition_score(text, n=4):
    toks = text.split()
    grams = [tuple(toks[i:i + n]) for i in range(max(len(toks) - n + 1, 0))]
    return 0.0 if not grams else 1 - len(set(grams)) / len(grams)


def json_valid(text):
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        json.loads(t)
        return True
    except Exception:
        return False


def auto_flags(r):
    return {"json_valid": json_valid(r["response"]) if r["format"] == "json" else None,
            "repetition_4gram": round(repetition_score(r["response"]), 3),
            "math_marker_count": len(MATH_PAT.findall(r["response"])), "n_new_tokens": r["n_new_tokens"],
            "truncated": r["truncated"]}


def build_annotation(rows, plain_order, conds):
    chosen = [plain_order[0], plain_order[-1]]
    sel = [r for r in rows if r["base"] in chosen]
    order = {c: i for i, c in enumerate(conds)}
    sel.sort(key=lambda r: (chosen.index(r["base"]), FORMATS.index(r["format"]), order[r["condition"]]))
    assert len(sel) == 42, len(sel)
    rd = results_dir("task1", "qualitative")
    cols = ["base", "format", "condition", "json_valid", "repetition_4gram", "math_marker_count", "n_new_tokens",
            "truncated", "ANN_format_compliance(0-2)", "ANN_coherence_relevance(0-2)", "ANN_repetition(0-2)",
            "ANN_unnecessary_math_framing(y/n)", "ANN_inappropriate_content(y/n)", "ANN_short_excerpt", "ANN_notes",
            "response"]
    with open(os.path.join(rd, "annotation_sheet_42.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sel:
            d = {"base": r["base"], "format": r["format"], "condition": r["condition"], **auto_flags(r),
                 "response": r["response"]}
            w.writerow({c: d.get(c, "") for c in cols})
    # side-by-side markdown for reading: grouped by question and format
    with open(os.path.join(rd, "side_by_side_all168.md"), "w") as f:
        bases = list(dict.fromkeys(r["base"] for r in rows))
        for b in bases:
            for fmt in FORMATS:
                grp = [r for r in rows if r["base"] == b and r["format"] == fmt]
                if not grp:
                    continue
                f.write(f"\n## {b} [{fmt}]\n\n**System:** {grp[0]['system']}\n\n**User:** {grp[0]['user']}\n\n")
                for r in sorted(grp, key=lambda r: order[r["condition"]]):
                    fl = auto_flags(r)
                    f.write(f"### {r['condition']}  (tokens={fl['n_new_tokens']}, trunc={fl['truncated']}, "
                            f"json_valid={fl['json_valid']}, rep4={fl['repetition_4gram']}, "
                            f"math_markers={fl['math_marker_count']})\n\n```\n{r['response']}\n```\n\n")
    # automatic-flag summary over all 168 (per condition x format)
    summ = {}
    for r in rows:
        key = f"{r['condition']}|{r['format']}"
        s = summ.setdefault(key, {"n": 0, "truncated": 0, "json_valid": 0, "mean_rep4": 0.0, "mean_math_markers": 0.0,
                                  "mean_tokens": 0.0})
        fl = auto_flags(r)
        s["n"] += 1
        s["truncated"] += int(fl["truncated"])
        s["json_valid"] += int(bool(fl["json_valid"]))
        s["mean_rep4"] += fl["repetition_4gram"]
        s["mean_math_markers"] += fl["math_marker_count"]
        s["mean_tokens"] += fl["n_new_tokens"]
    for s in summ.values():
        for kk in ["mean_rep4", "mean_math_markers", "mean_tokens"]:
            s[kk] /= s["n"]
    with open(os.path.join(rd, "auto_flag_summary.json"), "w") as f:
        json.dump(summ, f, indent=1)


def main(cfg, conds_only=None):
    prompts, plain_order = load_prompts(cfg["data"]["qualitative_yaml"])
    conds = list(cfg["eval"]["conditions"].keys())
    path = os.path.join(out_dir("task1", "qualitative"), "responses_168.jsonl")
    rows = read_jsonl(path)
    done = {r["condition"] for r in rows}
    for c in conds:
        if c in done or (conds_only and c not in conds_only):
            continue
        rows += generate_condition(cfg, c, prompts)
        write_jsonl(path, rows)
    write_jsonl(os.path.join(results_dir("task1", "qualitative"), "responses_168.jsonl"), rows)
    if {r["condition"] for r in rows} == set(conds):
        assert len(rows) == 168
        build_annotation(rows, plain_order, conds)
        print("annotation sheet written to results/task1/qualitative/")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task1.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    ap.add_argument("--conditions", nargs="*", default=None)
    a = ap.parse_args()
    main(load_config(a.config, a.set), a.conditions)
