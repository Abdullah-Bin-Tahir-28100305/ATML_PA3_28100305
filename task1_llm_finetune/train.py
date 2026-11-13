"""Task 1 training: Full FT, Full FT + L2-SP, LoRA-4, LoRA-16, OPLoRA-16 (one epoch, 1,250 updates each).

Precision: FP16 autocast + dynamic GradScaler. Full FT: FP32 master weights. Adapters: frozen FP16 backbone,
FP32 adapters. Optimizer states FP32. Gradients are unscaled before clipping; skipped (overflow) updates are logged.

Loss: next-token CE on response tokens + <|im_end|> only; the summed loss of all microbatches is divided by
the number of valid response tokens in the whole effective batch (32 examples).

L2-SP: Omega = 0.5 * sum_j (theta_j - theta0_j)^2 over all unique parameters (tied params once, biases and norms
included, no averaging). lambda*Omega is added to the objective once per optimizer update; we add its exact
gradient lambda*(theta - theta0) to the unscaled gradient before clipping (identical to differentiating the
penalty term, but avoids an extra FP16 backward over 0.6B parameters).

Usage:  python -m task1_llm_finetune.train --variant full_ft   (auto-resumes from the latest recovery ckpt)
"""
import argparse
import math
import os
import shutil
import time

import torch
import torch.nn.functional as F

from common.lora import (LLM_TARGETS, compute_all_protection_bases, count_all_unique, count_trainable,
                         export_bases, export_effective_factors, inject_lora, lora_modules)
from common.utils import (append_jsonl, dir_size_bytes, get_rng_state, load_config, load_json, out_dir,
                          peak_memory_mb, reset_peak_memory, results_dir, save_json, set_rng_state, set_seed)
from task1_llm_finetune.data import load_training_data, nothink_tokenizer_dir


def run_dir(variant):
    return out_dir("task1", "runs", variant)


def export_dir(variant, update):
    return os.path.join(run_dir(variant), f"export_u{update}")


def build_model(cfg, vcfg, device):
    from transformers import AutoModelForCausalLM
    full = vcfg["kind"] == "full"
    model = AutoModelForCausalLM.from_pretrained(cfg["model_id"], revision=cfg.get("model_revision"),
                                                 torch_dtype=torch.float32 if full else torch.float16,
                                                 attn_implementation="sdpa").to(device)
    model.config.use_cache = False
    extra = {}
    if not full:
        for p in model.parameters():
            p.requires_grad_(False)
        replaced = inject_lora(model, cfg.get("lora_targets", LLM_TARGETS), vcfg["r"], vcfg["alpha"],
                               vcfg["dropout"], vcfg.get("protect_k", 0), seed=cfg["seed"])
        extra["n_adapted_modules"] = len(replaced)
        if vcfg.get("protect_k", 0) > 0:
            extra.update(compute_all_protection_bases(model))
    if cfg["train"]["gradient_checkpointing"]:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.train()
    return model, extra


def make_optimizer(model, vcfg, tcfg):
    decay, no_decay, seen = [], [], set()
    for n, p in model.named_parameters():
        if not p.requires_grad or id(p) in seen:
            continue
        seen.add(id(p))
        # weight decay excludes biases and every parameter with fewer than two dimensions
        (decay if p.ndim >= 2 and not n.endswith(".bias") else no_decay).append(p)
    opt = torch.optim.AdamW([{"params": decay, "weight_decay": tcfg["weight_decay"]},
                             {"params": no_decay, "weight_decay": 0.0}],
                            lr=vcfg["lr"], betas=tuple(tcfg["betas"]), eps=tcfg["eps"])
    return opt, decay + no_decay


def collate(examples, pad_id, device):
    L = max(len(e["prompt_ids"]) + len(e["response_ids"]) for e in examples)
    ids = torch.full((len(examples), L), pad_id, dtype=torch.long)
    labels = torch.full((len(examples), L), -100, dtype=torch.long)
    attn = torch.zeros((len(examples), L), dtype=torch.long)
    for i, e in enumerate(examples):
        seq = e["prompt_ids"] + e["response_ids"]
        ids[i, : len(seq)] = torch.tensor(seq)
        attn[i, : len(seq)] = 1
        labels[i, len(e["prompt_ids"]): len(seq)] = torch.tensor(e["response_ids"])
    return ids.to(device), labels.to(device), attn.to(device)


def microbatch_loss_sum(model, ids, labels, attn):
    """Sum of token CE over supervised positions. lm_head is applied only to supervised positions (memory)."""
    with torch.autocast(device_type="cuda", dtype=torch.float16):
        h = model.model(input_ids=ids, attention_mask=attn).last_hidden_state
        h = h[:, :-1]
        tgt = labels[:, 1:]
        mask = tgt != -100
        logits = model.lm_head(h[mask])
    return F.cross_entropy(logits.float(), tgt[mask], reduction="sum"), int(mask.sum())


def unique_named_params(model):
    seen, out = set(), []
    for n, p in model.named_parameters():  # remove_duplicate=True -> tied embedding counted once
        if id(p) not in seen:
            seen.add(id(p))
            out.append((n, p))
    return out


@torch.no_grad()
def displacement(model, theta0):
    sq = 0.0
    for n, p in unique_named_params(model):
        if n in theta0:
            d = p.detach().float() - theta0[n].to(p.device, torch.float32)
            sq += float((d * d).sum())
    return math.sqrt(sq)


@torch.no_grad()
def save_diag_deltas(model, cfg, vcfg, theta0, path):
    """FP32 dW of down_proj in the diagnostic layers (used by diagnostics.py)."""
    layers = cfg["diagnostics"]["layers"]
    out = {}
    for li in layers:
        name = f"model.layers.{li}.mlp.{cfg['diagnostics']['module']}"
        if vcfg["kind"] == "full":
            W = dict(model.named_parameters())[name + ".weight"].detach().float().cpu()
            out[name] = W - theta0[name + ".weight"].float().cpu()
        else:
            out[name] = lora_modules(model)[name].delta_weight().detach().float().cpu()
    torch.save(out, path)


def save_export(model, cfg, vcfg, variant, update, theta0_cpu, metrics):
    """Inference export used by evaluation: FP16 full model (Full FT) or projected adapter factors."""
    from safetensors.torch import save_file
    d = export_dir(variant, update)
    os.makedirs(d, exist_ok=True)
    t0 = time.perf_counter()
    if vcfg["kind"] == "full":
        sd = {k: v.detach().half().cpu() for k, v in model.state_dict().items()}
        model.save_pretrained(d, state_dict=sd, safe_serialization=True)
        from transformers import AutoTokenizer
        AutoTokenizer.from_pretrained(nothink_tokenizer_dir(cfg)).save_pretrained(d)
    else:
        save_file(export_effective_factors(model), os.path.join(d, "adapter_effective_factors.safetensors"))
        if vcfg.get("protect_k", 0) > 0:
            bases = export_bases(model)
            save_file(bases, os.path.join(d, "oplora_bases.safetensors"))
        save_json({"r": vcfg["r"], "alpha": vcfg["alpha"], "protect_k": vcfg.get("protect_k", 0),
                   "targets": cfg.get("lora_targets", LLM_TARGETS), "base": cfg["model_id"]},
                  os.path.join(d, "adapter_config.json"))
    export_sec = time.perf_counter() - t0
    save_diag_deltas(model, cfg, vcfg, theta0_cpu, os.path.join(d, "diag_deltas.pt"))
    info = {"update": update, "export_sec": export_sec, "export_bytes": dir_size_bytes(d)}
    if vcfg["kind"] == "full":
        info["displacement_l2"] = displacement(model, theta0_cpu)
    else:
        f = os.path.join(d, "adapter_effective_factors.safetensors")
        info["adapter_file_bytes"] = os.path.getsize(f)
        if vcfg.get("protect_k", 0) > 0:
            info["basis_file_bytes"] = os.path.getsize(os.path.join(d, "oplora_bases.safetensors"))
    save_json(info, os.path.join(d, "export_info.json"))
    metrics.setdefault("exports", {})[str(update)] = info
    return info


def save_recovery(path, model, opt, sched, scaler, next_update, state):
    os.makedirs(path, exist_ok=True)
    t0 = time.perf_counter()
    trainable = {n: p.detach() for n, p in model.named_parameters() if p.requires_grad}
    tmp = os.path.join(path, "state.pt.tmp")
    torch.save({"trainable": trainable, "optimizer": opt.state_dict(), "scheduler": sched.state_dict(),
                "scaler": scaler.state_dict(), "rng": get_rng_state(), "next_update": next_update,
                "data_position": next_update * 32, "state": state}, tmp)
    os.replace(tmp, os.path.join(path, "state.pt"))
    return time.perf_counter() - t0, dir_size_bytes(path)


def latest_recovery(variant):
    rd = run_dir(variant)
    cks = sorted([d for d in os.listdir(rd) if d.startswith("recovery_u") and
                  os.path.exists(os.path.join(rd, d, "state.pt"))], key=lambda s: int(s[10:]))
    return os.path.join(rd, cks[-1]) if cks else None


def train(cfg, variant):
    from transformers import get_cosine_schedule_with_warmup
    assert torch.cuda.is_available(), "a CUDA GPU is required"
    device = torch.device("cuda")
    tcfg, vcfg = cfg["train"], cfg["variants"][variant]
    set_seed(cfg["seed"])
    rd = run_dir(variant)
    log_path = os.path.join(rd, "train_log.jsonl")
    data = load_training_data()
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(cfg["model_id"], revision=cfg.get("model_revision"))
    pad_id = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id

    reset_peak_memory()
    t_setup = time.perf_counter()
    model, extra = build_model(cfg, vcfg, device)
    opt, trainable_params = make_optimizer(model, vcfg, tcfg)
    sched = get_cosine_schedule_with_warmup(opt, tcfg["warmup_updates"], tcfg["num_updates"])
    scaler = torch.amp.GradScaler("cuda")
    full = vcfg["kind"] == "full"
    lam = float(vcfg.get("l2sp_lambda", 0.0))

    # theta0: CPU copy for displacement/diagnostics; for L2-SP an extra FP32 reference on `ref_device`
    theta0_cpu = {n: p.detach().float().cpu().clone() for n, p in unique_named_params(model)} if full else \
        {f"model.layers.{li}.mlp.down_proj.weight": dict(model.named_modules())[
            f"model.layers.{li}.mlp.down_proj"].base.weight.detach().float().cpu().clone()
         for li in cfg["diagnostics"]["layers"]}
    theta0_ref, ref_bytes = None, 0
    if lam > 0:
        ref_dev = vcfg.get("ref_device", "cuda")
        theta0_ref = {n: p.detach().float().clone().to(ref_dev) for n, p in unique_named_params(model)}
        ref_bytes = sum(v.numel() * 4 for v in theta0_ref.values())
    setup_sec = time.perf_counter() - t_setup

    state = {"train_sec": 0.0, "penalty_sec": 0.0, "ckpt_sec": 0.0, "skipped_updates": [],
             "setup_sec": setup_sec, "peak_mem_history": []}
    start = 0
    ck = latest_recovery(variant)
    if ck:
        blob = torch.load(os.path.join(ck, "state.pt"), map_location="cpu", weights_only=False)
        named = dict(model.named_parameters())
        with torch.no_grad():
            for n, v in blob["trainable"].items():
                named[n].copy_(v.to(named[n].device))
        opt.load_state_dict(blob["optimizer"])
        sched.load_state_dict(blob["scheduler"])
        scaler.load_state_dict(blob["scaler"])
        set_rng_state(blob["rng"])
        start, state = blob["next_update"], blob["state"]
        state.setdefault("resumed_at", []).append(start)
        print(f"[{variant}] resumed from {ck} at update {start}")
        del blob
    elif os.path.exists(log_path):
        os.remove(log_path)

    metrics_path = os.path.join(rd, "run_metrics.json")
    metrics = load_json(metrics_path) if (ck and os.path.exists(metrics_path)) else {}
    metrics.update({"variant": variant, "config": vcfg, "trainable_params": count_trainable(model),
                    "total_unique_params_in_model": count_all_unique(model), **extra,
                    "l2sp_reference_bytes": ref_bytes, "l2sp_reference_device": vcfg.get("ref_device")})
    if not full:
        metrics["adapter_params"] = metrics["trainable_params"]
    print(f"[{variant}] trainable params: {metrics['trainable_params']:,}")

    exports = tcfg["eval_export_updates"].get(variant, tcfg["eval_export_updates"]["default"])
    G, mb = tcfg["grad_accum"], tcfg["microbatch"]
    eff = G * mb
    for u in range(start, tcfg["num_updates"]):
        t0 = time.perf_counter()
        batch = data[u * eff:(u + 1) * eff]
        n_valid = sum(len(e["response_ids"]) for e in batch)
        lr_used = opt.param_groups[0]["lr"]
        opt.zero_grad(set_to_none=True)
        sft_sum = 0.0
        for m in range(G):
            ids, labels, attn = collate(batch[m * mb:(m + 1) * mb], pad_id, device)
            loss_sum, _ = microbatch_loss_sum(model, ids, labels, attn)
            scaler.scale(loss_sum / n_valid).backward()
            sft_sum += float(loss_sum.detach())
        scaler.unscale_(opt)
        omega = None
        if lam > 0:
            tp = time.perf_counter()
            with torch.no_grad():
                om = torch.zeros((), device=device, dtype=torch.float32)
                for n, p in unique_named_params(model):
                    d = p.detach() - theta0_ref[n].to(p.device, non_blocking=True)
                    om += 0.5 * (d * d).sum()
                    if p.grad is not None:
                        p.grad.add_(d, alpha=lam)
                omega = float(om)
            torch.cuda.synchronize()
            state["penalty_sec"] += time.perf_counter() - tp
        grad_norm = float(torch.nn.utils.clip_grad_norm_(trainable_params, tcfg["clip_norm"]))
        scale_before = scaler.get_scale()
        scaler.step(opt)
        scaler.update()
        skipped = scaler.get_scale() < scale_before
        if skipped:
            state["skipped_updates"].append(u + 1)
        sched.step()
        torch.cuda.synchronize()
        dt = time.perf_counter() - t0
        state["train_sec"] += dt
        sft = sft_sum / n_valid
        rec = {"update": u + 1, "examples_seen": (u + 1) * eff, "lr": lr_used, "sft_loss": sft,
               "grad_norm_preclip": grad_norm, "skipped": bool(skipped), "loss_scale": scale_before,
               "valid_tokens": n_valid, "step_sec": dt}
        if lam > 0:
            rec.update({"omega": omega, "lambda_omega": lam * omega, "total_loss": sft + lam * omega})
        append_jsonl(log_path, rec)
        if (u + 1) % 25 == 0 or u == start:
            print(f"[{variant}] u={u+1} loss={sft:.4f} lr={lr_used:.2e} gn={grad_norm:.2f} "
                  f"{'SKIP ' if skipped else ''}{'omega=%.3f' % omega if omega is not None else ''} {dt:.1f}s")

        if (u + 1) in exports:
            save_export(model, cfg, vcfg, variant, u + 1, theta0_cpu, metrics)
            if lam > 0:
                metrics["exports"][str(u + 1)]["omega"] = omega
        if (u + 1) in tcfg["recovery_updates"]:
            state["peak_mem_history"].append({"update": u + 1, **peak_memory_mb()})
            sec, size = save_recovery(os.path.join(rd, f"recovery_u{u+1}"), model, opt, sched, scaler, u + 1, state)
            state["ckpt_sec"] += sec
            metrics.setdefault("recovery_ckpt_bytes", {})[str(u + 1)] = size
            if not tcfg.get("keep_all_recovery", False):
                for d in os.listdir(rd):
                    if d.startswith("recovery_u") and d != f"recovery_u{u+1}":
                        shutil.rmtree(os.path.join(rd, d), ignore_errors=True)
            metrics.update({"train_sec": state["train_sec"], "skipped_updates": state["skipped_updates"]})
            save_json(metrics, metrics_path)

    pk = peak_memory_mb()
    metrics.update({"train_sec": state["train_sec"], "train_hours": state["train_sec"] / 3600,
                    "l2sp_penalty_sec": state["penalty_sec"], "checkpoint_save_sec": state["ckpt_sec"],
                    "setup_sec": state["setup_sec"], "peak_gpu_mem_mb_this_session": pk,
                    "peak_gpu_mem_mb_max_over_sessions": max([pk["allocated_mb"]] + [h["allocated_mb"] for h in
                                                                                   state["peak_mem_history"]]),
                    "skipped_updates": state["skipped_updates"], "n_skipped": len(state["skipped_updates"]),
                    "resumed_at": state.get("resumed_at", []), "gpu": torch.cuda.get_device_name(0)})
    save_json(metrics, metrics_path)
    save_json(metrics, os.path.join(results_dir("task1", "runs"), f"{variant}_run_metrics.json"))
    shutil.copy(log_path, os.path.join(results_dir("task1", "runs"), f"{variant}_train_log.jsonl"))
    print(f"[{variant}] done. {metrics['train_hours']:.2f} h, peak {pk['allocated_mb']:.0f} MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task1.yaml")
    ap.add_argument("--variant", required=True, choices=["full_ft", "l2sp", "lora4", "lora16", "oplora16"])
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    train(load_config(a.config, a.set), a.variant)
