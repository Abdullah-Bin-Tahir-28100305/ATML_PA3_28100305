"""Task 1 weight-space and activation diagnostics (FP32).

For down_proj in layers 0, 13, 27 and checkpoints {Full FT mid, Full FT, L2-SP, LoRA-4, LoRA-16, OPLoRA-16}:
  rho_k = ||U_k U_k^T dW||_F^2 / ||dW||_F^2       (fraction of update in protected OUTPUT subspace, k=128)
  rho_k_in = ||dW V_k V_k^T||_F^2 / ||dW||_F^2    (extra: protected INPUT subspace)
  eta   = ||dW||_F / ||W0||_F
  R(z)  = ||dW z||_2 / (||W0 z||_2 + 1e-8)        on cached starting-model down_proj inputs at the last prompt token
Bases U_k, V_k come from the same starting weights for every method (SVD in FP32 of the FP16 backbone weight,
which is exactly what OPLoRA used). OPLoRA constraint verification is reported separately.

Usage:  python -m task1_llm_finetune.diagnostics
"""
import argparse
import os

import numpy as np
import torch

from common.utils import load_config, load_json, out_dir, results_dir, save_json, write_jsonl
from task1_llm_finetune.train import export_dir

CKPTS = {"full_ft_mid": ("full_ft", 625), "full_ft": ("full_ft", 1250), "l2sp": ("l2sp", 1250),
         "lora4": ("lora4", 1250), "lora16": ("lora16", 1250), "oplora16": ("oplora16", 1250)}


def mod_name(li, module="down_proj"):
    return f"model.layers.{li}.mlp.{module}"


@torch.no_grad()
def load_base_weights(cfg):
    from transformers import AutoModelForCausalLM
    m = AutoModelForCausalLM.from_pretrained(cfg["model_id"], revision=cfg.get("model_revision"),
                                             torch_dtype=torch.float32)
    named = dict(m.named_parameters())
    W0_32 = {mod_name(li): named[mod_name(li) + ".weight"].detach().clone() for li in cfg["diagnostics"]["layers"]}
    return m, W0_32


@torch.no_grad()
def bases_for(W0_32, k):
    out = {}
    for n, W in W0_32.items():
        W16 = W.half().float()  # OPLoRA computed bases from the FP16 backbone weight, in FP32
        U, S, Vh = torch.linalg.svd(W16.cuda() if torch.cuda.is_available() else W16, full_matrices=False)
        out[n] = (U[:, :k].cpu(), Vh[:k].T.cpu(), S.cpu())
    return out


@torch.no_grad()
def cache_activations(cfg, model):
    """Forward hooks on the STARTING model's down_proj; input at the last non-padding prompt token (batch=1)."""
    from transformers import AutoTokenizer
    from task1_llm_finetune.data import nothink_tokenizer_dir
    tok = AutoTokenizer.from_pretrained(nothink_tokenizer_dir(cfg))
    probes = load_json(os.path.join(results_dir("task1", "splits"), "activation_probe_set.json"))["probes"]
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(dev).eval()
    layers = cfg["diagnostics"]["layers"]
    store, hooks = {}, []
    mods = dict(model.named_modules())
    for li in layers:
        def hook(mod, inp, out, _li=li):
            store[_li] = inp[0][0, -1].detach().float().cpu()
        hooks.append(mods[mod_name(li)].register_forward_hook(hook))
    Z = {li: [] for li in layers}
    meta = []
    for p in probes:
        if p["chat"]:  # GSM8K benchmark format: chat template (non-thinking) + generation prompt
            text = tok.apply_chat_template([{"role": "user", "content": p["text"]}], tokenize=False,
                                           add_generation_prompt=True)
        else:          # HellaSwag stock completion context, no template
            text = p["text"]
        ids = tok(text, return_tensors="pt", add_special_tokens=False).input_ids.to(dev)
        model(input_ids=ids)
        for li in layers:
            Z[li].append(store[li])
        meta.append({"set": p["set"], "id": p["id"], "n_tokens": ids.shape[1]})
    for h in hooks:
        h.remove()
    Z = {li: torch.stack(v) for li, v in Z.items()}
    torch.save({"Z": Z, "meta": meta}, os.path.join(out_dir("task1", "diagnostics"), "probe_activations.pt"))
    return Z, meta


def load_delta(cfg, ckpt):
    variant, update = CKPTS[ckpt]
    path = os.path.join(export_dir(variant, update), "diag_deltas.pt")
    return torch.load(path, map_location="cpu")


def run(cfg):
    k = cfg["diagnostics"]["protect_k"]
    layers = cfg["diagnostics"]["layers"]
    model, W0 = load_base_weights(cfg)
    B = bases_for(W0, k)
    act_path = os.path.join(out_dir("task1", "diagnostics"), "probe_activations.pt")
    if os.path.exists(act_path):
        blob = torch.load(act_path)
        Z, meta = blob["Z"], blob["meta"]
    else:
        Z, meta = cache_activations(cfg, model)
    del model
    sets = np.array([m["set"] for m in meta])
    weight_rows, act_rows, per_example = [], [], []
    for ckpt in CKPTS:
        try:
            deltas = load_delta(cfg, ckpt)
        except FileNotFoundError:
            print("missing", ckpt)
            continue
        for li in layers:
            n = mod_name(li)
            dW = deltas[n].float()
            W = W0[n].float()
            Uk, Vk, _ = B[n]
            fro2 = float((dW ** 2).sum())
            rho = float(((Uk @ (Uk.T @ dW)) ** 2).sum()) / max(fro2, 1e-30)
            rho_in = float((((dW @ Vk) @ Vk.T) ** 2).sum()) / max(fro2, 1e-30)
            eta = float(dW.norm()) / float(W.norm())
            sv = torch.linalg.svdvals(dW)
            eff_rank = float((sv.sum() ** 2) / (sv ** 2).sum()) if fro2 > 0 else 0.0
            weight_rows.append({"ckpt": ckpt, "layer": li, "rho_k": rho, "rho_k_input": rho_in, "eta": eta,
                                "dW_fro": float(dW.norm()), "W0_fro": float(W.norm()),
                                "dW_effective_rank": eff_rank, "dW_top_sv": float(sv[0])})
            z = Z[li].float()
            num = (z @ dW.T).norm(dim=1)
            den = (z @ W.T).norm(dim=1) + 1e-8
            R = (num / den).numpy()
            for i, m in enumerate(meta):
                per_example.append({"ckpt": ckpt, "layer": li, "set": m["set"], "id": m["id"], "R": float(R[i])})
            for s in ["math", "nonmath"]:
                r = R[sets == s]
                act_rows.append({"ckpt": ckpt, "layer": li, "set": s, "R_mean": float(r.mean()),
                                 "R_median": float(np.median(r)), "R_std": float(r.std()),
                                 "R_q25": float(np.quantile(r, .25)), "R_q75": float(np.quantile(r, .75))})
            act_rows.append({"ckpt": ckpt, "layer": li, "set": "math/nonmath ratio",
                             "R_mean": float(R[sets == "math"].mean() / max(R[sets == "nonmath"].mean(), 1e-12))})

    # OPLoRA constraint verification (numerical, separate from performance evidence)
    verify = {}
    ed = export_dir("oplora16", 1250)
    if os.path.exists(os.path.join(ed, "adapter_effective_factors.safetensors")):
        from safetensors.torch import load_file
        fac = load_file(os.path.join(ed, "adapter_effective_factors.safetensors"))
        bases = load_file(os.path.join(ed, "oplora_bases.safetensors"))
        names = sorted({kk.rsplit(".", 1)[0] for kk in fac if kk.endswith(".lora_A")})
        maxB = maxA = 0.0
        for nm in names:
            A, Bf = fac[nm + ".lora_A"].float(), fac[nm + ".lora_B"].float()
            U, V = bases[nm + ".U_k"].float(), bases[nm + ".V_k"].float()
            maxB = max(maxB, float((U.T @ Bf).norm() / max(Bf.norm(), 1e-30)))
            maxA = max(maxA, float((A @ V).norm() / max(A.norm(), 1e-30)))
        verify["max_rel_norm_UkT_Btilde"] = maxB
        verify["max_rel_norm_Atilde_Vk"] = maxA
        for li in layers:
            n = mod_name(li)
            U_tr = bases[n + ".U_k"].float()
            # subspace agreement between training bases and diagnostic bases (1.0 = identical subspace)
            verify[f"layer{li}_subspace_overlap"] = float((B[n][0].T @ U_tr).norm() ** 2 / k)
        verify["n_adapted_modules"] = len(names)
    rd = results_dir("task1", "diagnostics")
    save_json({"weight": weight_rows, "activation": act_rows, "oplora_verification": verify}, os.path.join(rd, "diagnostics.json"))
    write_jsonl(os.path.join(rd, "activation_per_example.jsonl"), per_example)
    print("weight:", *weight_rows, sep="\n")
    print("oplora verification:", verify)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task1.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    run(load_config(a.config, a.set))
