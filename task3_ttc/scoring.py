"""Quality scores and answer aggregation (everything computed from the CURRENT candidate prefix only).

Per rollout i (T_i tokens, termination excluded):
  L_i = mean_t log p(y_t)                 (sampled-token log-probability; column `logp`)
  K_i = mean_t KL(U || p_t)               (self-certainty; column `kl_u_p`)
  V_i = mean_t A_img / (A_img + A_prompt + A_gen)        (visual engagement, MathVista)
VisConf (per prefix G): c_it = clip01((k_it - Q05(G)) / max(Q95(G) - Q05(G), 1e-8)) with linear-interpolation
percentiles over ALL tokens pooled across the current candidates; C_i = mean_t c_it; Z_i = V_i * C_i.
Tokenwise fusion ablation: Z_tok_i = mean_t c_it * v_it  (= Z_i + Cov_t(c, v)).
Selection: highest score (ties -> earliest rollout in the current order) or rank-weighted consensus
  w_i = (N - r_i)/(N - 1) with average ranks (rank 1 = best), W(a) = sum_{i: a_i = a} w_i, ties -> earliest supporter.
Self-consistency: one vote per rollout, ties -> earliest supporter. N = 1 -> every rule returns the only candidate.
"""
import numpy as np
from scipy.stats import rankdata


def _clean(s):
    s = np.asarray(s, dtype=float)
    return np.where(np.isfinite(s), s, -np.inf)


def argmax_earliest(scores):
    s = _clean(scores)
    return int(np.argmax(s))  # np.argmax returns the first maximal index = earliest in the current order


def vote(labels, weights=None):
    """Return (winner, W dict, first-position dict). Ties broken by earliest supporting rollout."""
    W, first = {}, {}
    for i, l in enumerate(labels):
        W[l] = W.get(l, 0.0) + (1.0 if weights is None else float(weights[i]))
        first.setdefault(l, i)
    win = max(W, key=lambda l: (W[l], -first[l]))
    return win, W, first


def rwc(labels, scores):
    N = len(labels)
    if N == 1:
        return labels[0], {labels[0]: 1.0}
    r = rankdata(-_clean(scores), method="average")  # rank 1 = best; ties get average ranks
    w = (N - r) / (N - 1)
    win, W, _ = vote(labels, w)
    return win, W


def rwc_margin(W):
    v = sorted(W.values(), reverse=True)
    tot = sum(v)
    if tot <= 0:
        return 0.0
    return (v[0] - (v[1] if len(v) > 1 else 0.0)) / tot


def visconf_parts(q, pos):
    """Calibrated confidence for the candidate prefix `pos` (array of positions into q's rollouts)."""
    kls = [q["kl"][i] for i in pos]
    pooled = np.concatenate([k for k in kls if len(k)]) if any(len(k) for k in kls) else np.zeros(1)
    lo, hi = np.percentile(pooled, [5, 95])  # numpy default = linear interpolation
    den = max(hi - lo, 1e-8)
    C, Ztok, cs = [], [], []
    for i, k in zip(pos, kls):
        if len(k) == 0:
            C.append(-np.inf), Ztok.append(-np.inf), cs.append(np.zeros(0))
            continue
        c = np.clip((k - lo) / den, 0.0, 1.0)
        cs.append(c)
        C.append(c.mean())
        Ztok.append((c * q["v"][i]).mean() if "v" in q else np.nan)
    C = np.array(C)
    out = {"C": C, "Ztok": np.array(Ztok), "c_tokens": cs, "q05": lo, "q95": hi}
    if "V" in q:
        out["Z"] = q["V"][pos] * np.where(np.isfinite(C), C, 0) + np.where(np.isfinite(C), 0, -np.inf)
    return out


def all_selections(q, pos, vision=False):
    """Return {method: chosen group label} for candidate prefix `pos` (ordered positions)."""
    pos = np.asarray(pos)
    labels = [q["labels"][i] for i in pos]
    out = {"FirstAns": labels[0]}
    if len(pos) == 1:
        names = ["SC", "LogP", "SelfCert", "SelfCert+RWC", "Shortest"]
        if vision:
            names += ["Vision", "Vision+RWC", "VisConf", "VisConf+RWC", "TokFusion", "TokFusion+RWC"]
        return {**out, **{n: labels[0] for n in names}}
    out["SC"] = vote(labels)[0]
    out["LogP"] = labels[argmax_earliest(q["L"][pos])]
    out["SelfCert"] = labels[argmax_earliest(q["K"][pos])]
    out["SelfCert+RWC"] = rwc(labels, q["K"][pos])[0]
    T = q["T"][pos].astype(float)
    out["Shortest"] = labels[int(np.argmin(T))]  # length-only control (ties -> earliest)
    if vision:
        vc = visconf_parts(q, pos)
        out["Vision"] = labels[argmax_earliest(q["V"][pos])]
        out["Vision+RWC"] = rwc(labels, q["V"][pos])[0]
        out["VisConf"] = labels[argmax_earliest(vc["Z"])]
        out["VisConf+RWC"] = rwc(labels, vc["Z"])[0]
        out["TokFusion"] = labels[argmax_earliest(vc["Ztok"])]
        out["TokFusion+RWC"] = rwc(labels, vc["Ztok"])[0]
    return out


def quality_scores(q, pos, kind):
    """Score used by the quality-aware stopping rule: self-certainty (MATH) or VisConf (MathVista)."""
    if kind == "mathvista":
        return visconf_parts(q, np.asarray(pos))["Z"]
    return q["K"][np.asarray(pos)]
