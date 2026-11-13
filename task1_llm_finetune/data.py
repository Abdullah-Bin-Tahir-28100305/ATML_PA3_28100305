"""Task 1 data preparation.

* Selects the first 40,000 eligible MetaMathQA examples in source order (no shuffling, no truncation).
  Eligible = non-empty query and response, formatted length <= 2,048 tokens, and neither `query` nor
  `original_question` matches a GSM8K test question after normalization (lowercase, non-alphanumerics -> space,
  collapse whitespace; exact match).
* Formatting: prompt = Qwen chat template for a single user turn with add_generation_prompt=True and
  enable_thinking=False (this ends with "<|im_start|>assistant\n<think>\n\n</think>\n\n", i.e. exactly what the
  model sees at inference time). Target = response tokens + <|im_end|>. Loss is computed only on the target.
* Builds the activation probe set (32 GSM8K test questions + 32 HellaSwag validation contexts, seed 6304) and
  a tokenizer copy whose chat template forces enable_thinking=False (used by lm-eval and generation).

Usage:  python -m task1_llm_finetune.data --config configs/task1.yaml
"""
import argparse
import os
import pickle
import re

import numpy as np

from common.utils import load_config, normalize_question, out_dir, results_dir, save_json, set_seed


def nothink_tokenizer_dir(cfg):
    return os.path.join(out_dir("task1"), "tokenizer_nothink")


def make_nothink_tokenizer(cfg):
    """Save a tokenizer whose chat template always disables thinking (robust across lm-eval versions)."""
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(cfg["model_id"], revision=cfg.get("model_revision"))
    if not tok.chat_template.startswith("{%- set enable_thinking = false %}"):
        tok.chat_template = "{%- set enable_thinking = false %}" + tok.chat_template
    path = nothink_tokenizer_dir(cfg)
    tok.save_pretrained(path)
    # sanity check: generation prompt contains the empty think block
    s = tok.apply_chat_template([{"role": "user", "content": "hi"}], tokenize=False, add_generation_prompt=True)
    assert s.endswith("<think>\n\n</think>\n\n"), repr(s[-60:])
    return path


def format_example(tok, query, response, im_end_id):
    prompt = tok.apply_chat_template([{"role": "user", "content": query}], tokenize=False,
                                     add_generation_prompt=True, enable_thinking=False)
    p_ids = tok(prompt, add_special_tokens=False).input_ids
    r_ids = tok(response, add_special_tokens=False).input_ids + [im_end_id]
    return p_ids, r_ids


def tokenized_data_path():
    return os.path.join(out_dir("task1", "data"), "metamath_40k_tokenized.pkl")


def build_training_data(cfg):
    from datasets import load_dataset
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(cfg["model_id"], revision=cfg.get("model_revision"))
    im_end_id = tok.convert_tokens_to_ids("<|im_end|>")
    gsm = load_dataset(cfg["data"]["gsm8k"], "main", split="test")
    gsm_norm = {normalize_question(q) for q in gsm["question"]}
    ds = load_dataset(cfg["data"]["dataset"], split="train")
    N, max_len = cfg["data"]["num_examples"], cfg["data"]["max_len"]
    kept, stats = [], {"scanned": 0, "empty": 0, "too_long": 0, "gsm8k_overlap": 0}
    for i, ex in enumerate(ds):
        stats["scanned"] += 1
        q, r = ex.get("query") or "", ex.get("response") or ""
        if not q.strip() or not r.strip():
            stats["empty"] += 1
            continue
        if normalize_question(q) in gsm_norm or normalize_question(ex.get("original_question") or "") in gsm_norm:
            stats["gsm8k_overlap"] += 1
            continue
        p_ids, r_ids = format_example(tok, q, r, im_end_id)
        if len(p_ids) + len(r_ids) > max_len:
            stats["too_long"] += 1
            continue
        kept.append({"src_idx": i, "type": ex.get("type"), "prompt_ids": p_ids, "response_ids": r_ids})
        if len(kept) == N:
            break
    assert len(kept) == N, f"only {len(kept)} eligible examples"
    lens = np.array([len(k["prompt_ids"]) + len(k["response_ids"]) for k in kept])
    rlens = np.array([len(k["response_ids"]) for k in kept])
    stats.update({"kept": len(kept), "last_src_idx": kept[-1]["src_idx"], "mean_len": float(lens.mean()),
                  "max_len": int(lens.max()), "mean_response_tokens": float(rlens.mean()),
                  "total_response_tokens": int(rlens.sum()),
                  "type_counts": {t: int(c) for t, c in zip(*np.unique([k["type"] for k in kept], return_counts=True))}})
    with open(tokenized_data_path(), "wb") as f:
        pickle.dump(kept, f)
    save_json({"src_indices": [k["src_idx"] for k in kept]}, os.path.join(results_dir("task1", "splits"),
                                                                           "metamath_selected_indices.json"))
    save_json(stats, os.path.join(results_dir("task1", "splits"), "metamath_selection_stats.json"))
    print(stats)
    return kept


def load_training_data():
    with open(tokenized_data_path(), "rb") as f:
        return pickle.load(f)


# ----------------------------------------------------------------------------- probe set (benchmark prompt formats)
def _hellaswag_preprocess(text):
    # identical to lm-eval's hellaswag utils.preprocess
    text = text.strip()
    text = text.replace(" [title]", ". ")
    text = re.sub("\\[.*?\\]", "", text)
    text = text.replace("  ", " ")
    return text


def build_probe_set(cfg):
    """Sampled with seed 6304 BEFORE looking at any model results; IDs saved and reused for every model."""
    from datasets import load_dataset
    rng = np.random.default_rng(cfg["seed"])
    n = cfg["diagnostics"]["probe_n_per_set"]
    gsm = load_dataset(cfg["data"]["gsm8k"], "main", split="test")
    hs = load_dataset("Rowan/hellaswag", split="validation")
    gsm_ids = sorted(int(i) for i in rng.choice(len(gsm), n, replace=False))
    hs_ids = sorted(int(i) for i in rng.choice(len(hs), n, replace=False))
    probes = []
    for i in gsm_ids:
        # gsm8k_cot zero-shot doc_to_text, wrapped in the (non-thinking) chat template as the user turn
        probes.append({"set": "math", "id": i, "text": f"Q: {gsm[i]['question']}\nA:", "chat": True})
    for i in hs_ids:
        d = hs[i]
        ctx = d["ctx_a"] + " " + d["ctx_b"].capitalize()
        probes.append({"set": "nonmath", "id": i, "text": _hellaswag_preprocess(d["activity_label"] + ": " + ctx),
                       "chat": False})
    save_json({"gsm8k_test_ids": gsm_ids, "hellaswag_val_ids": hs_ids, "probes": probes},
              os.path.join(results_dir("task1", "splits"), "activation_probe_set.json"))
    return probes


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task1.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    cfg = load_config(a.config, a.set)
    set_seed(cfg["seed"])
    make_nothink_tokenizer(cfg)
    build_probe_set(cfg)
    if not os.path.exists(tokenized_data_path()):
        build_training_data(cfg)
    else:
        print("tokenized data exists:", tokenized_data_path())
