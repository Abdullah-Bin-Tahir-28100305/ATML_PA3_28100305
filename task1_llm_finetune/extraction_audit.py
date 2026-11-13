"""GSM8K extraction audit (post-hoc, does not change the primary metric).

lm-eval's gsm8k_cot 'flexible-extract' filter takes the LAST match of (-?[$0-9.,]{2,})|(-?[0-9]+). The LaTeX
display-math delimiter "$$" matches that pattern, so a response ending in  $$ \\boxed{120} $$  is extracted as "$$" and
scored wrong even though the stated answer is correct. This audit quantifies that effect per condition so extraction
changes can be separated from correctness changes (PA3 Task 1, "What to Watch For").

Secondary (format-aware) extractor, applied identically to every condition: last balanced \\boxed{...}; else the last
'#### X' / 'The answer is X' / 'Final Answer: X' / '**Answer: X' line; else the last number in the response.
Numbers are compared after removing commas, '$' and '%'. The primary metric remains flexible-extract EM.

Usage:  python -m task1_llm_finetune.extraction_audit
Output: results/task1/analysis/gsm8k_extraction_audit.csv
"""
import os
import re

import pandas as pd

from common.utils import read_jsonl, results_dir

CONDS = ["baseline", "full_ft_mid", "full_ft", "l2sp", "lora4", "lora16", "oplora16"]


def _num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("$", "").replace("\\%", "").replace("%", "").strip().rstrip(".")
    m = re.findall(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    v = float(m[-1])
    return str(int(v)) if v == int(v) else str(v)


def _last_boxed(t):
    i = t.rfind("\\boxed{")
    if i < 0:
        return None
    i += len("\\boxed{")
    depth = 1
    for j in range(i, len(t)):
        if t[j] == "{":
            depth += 1
        elif t[j] == "}":
            depth -= 1
            if depth == 0:
                return t[i:j]
    return None


def format_aware(t):
    t = t or ""
    b = _last_boxed(t)
    if b is not None and _num(b) is not None:
        return _num(b)
    for pat in [r"####\s*([^\n]+)", r"[Tt]he (?:final )?answer is:?\s*([^\n]+)", r"Final Answer[:\*\s]*([^\n]+)",
                r"\*\*Answer[:\*\s]*([^\n]+)"]:
        m = re.findall(pat, t)
        if m and _num(m[-1]) is not None:
            return _num(m[-1])
    return _num(t)


def main():
    rows = []
    for c in CONDS:
        R = read_jsonl(os.path.join(results_dir("task1", "eval", c), "gsm8k_rows.jsonl"))
        if not R:
            continue
        gold = [_num(str(r["target"]).split("####")[-1]) for r in R]
        flex = [bool(r.get("exact_match_flexible-extract")) for r in R]
        fa = [format_aware(r["response"]) == g for r, g in zip(R, gold)]
        nonnum = [not re.search(r"\d", str(r.get("extracted_flexible-extract"))) for r in R]
        n = len(R)
        rows.append({"condition": c, "n": n, "flexible_EM": 100 * sum(flex) / n,
                     "format_aware_EM": 100 * sum(fa) / n,
                     "non_numeric_flexible_extractions_pct": 100 * sum(nonnum) / n,
                     "flex_wrong_but_format_aware_right": sum(1 for f, a in zip(flex, fa) if not f and a),
                     "flex_right_but_format_aware_wrong": sum(1 for f, a in zip(flex, fa) if f and not a),
                     "has_boxed_pct": 100 * sum("\\boxed" in (r["response"] or "") for r in R) / n,
                     "truncated_pct": 100 * sum(bool(r["truncated"]) for r in R) / n})
    df = pd.DataFrame(rows)
    base = df.set_index("condition").loc["baseline"]
    df["delta_flexible_pp"] = df.flexible_EM - base.flexible_EM
    df["delta_format_aware_pp"] = df.format_aware_EM - base.format_aware_EM
    out = os.path.join(results_dir("task1", "analysis"), "gsm8k_extraction_audit.csv")
    df.to_csv(out, index=False, float_format="%.2f")
    print(df.round(2).to_string(index=False))
    print("written:", out)


if __name__ == "__main__":
    main()
