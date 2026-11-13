"""Answer normalization, equivalence grouping and grading (gold answers are NEVER used to form groups).

MATH:      equivalence via math-verify (parse + verify). Unique extracted strings are clustered greedily in a fixed,
           gold-independent order (frequency desc, then string) against each cluster's representative. A group is
           graded by verifying its representative against the gold answer, so voting and grading use the same rule.
MathVista: MathVista's own normalization (normalize_extracted_answer): multiple-choice -> option LETTER (letter given
           directly, exact choice text, or the most similar choice by edit distance, as in the official code);
           integer -> int(float(x)); float -> round(float(x), precision); list -> str; text -> stripped string.
           Equivalence = equality of normalized strings; gold normalized with the same rules.
Missing extractions -> one NO_ANSWER group, always graded incorrect, kept in voting/cost/accuracy.
"""
import re

NO_ANSWER = "NO_ANSWER"
SCORER_VERSION = "pa3-scorer-1.0"


def is_missing(x):
    return x is None or (isinstance(x, float) and x != x) or str(x).strip() == "" or str(x).strip().lower() in ("none", "null")


# ----------------------------------------------------------------------------- balanced-brace boxed extraction
def last_boxed(text):
    """Same rule as the supplied export (README): find the LAST literal '\\boxed{', scan balanced braces, strip
    whitespace. Absent, empty or unclosed last box -> None; no numeric fallback."""
    if text is None:
        return None
    i = text.rfind("\\boxed{")
    if i < 0:
        return None
    i += len("\\boxed{")
    depth = 1
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                out = text[i:j].strip()
                return out or None
    return None


# ----------------------------------------------------------------------------- MATH via math-verify
_PARSE_CACHE = {}


def _mv_parse(s):
    from math_verify import parse
    if s in _PARSE_CACHE:
        return _PARSE_CACHE[s]
    p = []
    for cand in (f"${s}$", f"\\boxed{{{s}}}", s):
        try:
            p = parse(cand)
        except Exception:
            p = []
        if p:
            break
    _PARSE_CACHE[s] = p
    return p


def math_equal(a, b):
    from math_verify import verify
    if a == b:
        return True
    pa, pb = _mv_parse(a), _mv_parse(b)
    if not pa or not pb:
        return a.replace(" ", "") == b.replace(" ", "")
    try:
        return bool(verify(pa, pb))
    except Exception:
        return False


def group_math(extracted):
    """extracted: list of raw strings (or None) for one question -> (group label per rollout, {label: rep})."""
    clean = [NO_ANSWER if is_missing(x) else str(x).strip().strip("$").strip() for x in extracted]
    counts = {}
    for c in clean:
        counts[c] = counts.get(c, 0) + 1
    uniq = sorted([u for u in counts if u != NO_ANSWER], key=lambda u: (-counts[u], u))
    reps, label_of = [], {NO_ANSWER: NO_ANSWER}
    for u in uniq:
        for r in reps:
            if math_equal(r, u):
                label_of[u] = r
                break
        else:
            reps.append(u)
            label_of[u] = u
    return [label_of[c] for c in clean], reps


def grade_math(rep, gold):
    if rep == NO_ANSWER or is_missing(gold):
        return False
    from math_verify import verify
    g = _mv_parse(str(gold).strip().strip("$"))
    p = _mv_parse(rep)
    if not g or not p:
        return rep.replace(" ", "") == str(gold).replace(" ", "")
    try:
        return bool(verify(g, p))
    except Exception:
        return False


# ----------------------------------------------------------------------------- MathVista
def _lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def mathvista_normalize(x, meta):
    """Official MathVista normalize_extracted_answer, except multiple-choice is represented by the option LETTER.
    Unparseable integer/float extractions become None in the official code -> NO_ANSWER here."""
    if is_missing(x):
        return NO_ANSWER
    s = str(x).strip()
    if meta["question_type"] == "multi_choice":
        choices = [str(c).strip() for c in meta["choices"]]
        letters = [chr(65 + i) for i in range(len(choices))]
        par = re.findall(r"\(([a-zA-Z])\)", s)           # official: "(A) text" -> "A"
        if par:
            s = par[0].upper()
        m = re.fullmatch(r"([A-Za-z])[\.\):]?", s)
        if m and m.group(1).upper() in letters:
            return m.group(1).upper()
        for i, c in enumerate(choices):
            if s.lower() == c.lower():
                return letters[i]
        d = [_lev(s, c) for c in choices]                 # official: most similar choice (edit distance)
        return letters[d.index(min(d))]
    at = meta.get("answer_type")
    try:
        if at == "integer":
            return str(int(float(s)))
        if at == "float":
            prec = meta.get("precision")
            prec = 0 if prec is None or prec != prec else int(prec)
            return str(round(float(s), prec))
    except (ValueError, OverflowError, TypeError):
        return NO_ANSWER
    return s


def mathvista_gold(gold, meta):
    if meta["question_type"] == "multi_choice":
        choices = [str(c).strip() for c in meta["choices"]]
        g = str(gold).strip()
        if g in choices:
            return chr(65 + choices.index(g))
    return mathvista_normalize(gold, meta)


def group_mathvista(extracted, meta):  # noqa: D401
    labels = [mathvista_normalize(x, meta) for x in extracted]
    return labels, sorted({l for l in labels if l != NO_ANSWER})


def grade_mathvista(label, gold_norm):
    return label != NO_ANSWER and label == gold_norm
