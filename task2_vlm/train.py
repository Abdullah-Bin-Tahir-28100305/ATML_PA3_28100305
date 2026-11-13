"""Task 2 curriculum training: Stage 1 caption alignment (connector only) -> Stage 2 visual instruction tuning
(connector + LoRA r16 on q/k/v/o) -> Stage 3 natural VQA specialization. One continuing model, 4,500 updates,
72,000 example presentations (incl. replay).

FP16 autocast + dynamic GradScaler; frozen FP16 Qwen backbone; FP32 connector/LoRA/optimizer states; unscale
before clipping (global norm 1.0). Per-stage schedule: 5% linear warmup then cosine decay to 10% of each peak LR.
A fresh AdamW is created at each stage boundary (the trainable set changes at Stage 2) - see README.
Resumable checkpoints every 250 updates (connector, adapters, optimizer, scheduler, scaler, sampler, stage
progress, RNG); connector/adapter weights saved after each stage.

Usage:  python -m task2_vlm.train          (auto-resumes)
"""
import argparse
import math
import os
import shutil
import time

import torch

from common.lora import count_trainable
from common.utils import (append_jsonl, get_rng_state, load_config, out_dir, peak_memory_mb, read_jsonl,
                          reset_peak_memory, results_dir, save_json, set_rng_state, set_seed)
from task2_vlm.cache_features import FeatureStore
from task2_vlm.data import CurriculumSampler, load_manifests
from task2_vlm.evaluate import dev_eval
from task2_vlm.model import VLM, calibrate_connector


def run_dir(cfg):
    return out_dir("task2", cfg["run_name"])


def stage_lambda(n_updates, warmup_frac, final_frac):
    W = max(1, math.ceil(warmup_frac * n_updates))

    def f(step):
        if step < W:
            return (step + 1) / W
        prog = min(1.0, (step - W) / max(1, n_updates - W))
        return final_frac + (1 - final_frac) * 0.5 * (1 + math.cos(math.pi * prog))
    return f


def make_opt(vlm, stage, tcfg):
    groups = []
    conn = list(vlm.connector.parameters())
    groups.append({"params": [p for p in conn if p.ndim >= 2], "lr": stage["connector_lr"],
                   "weight_decay": tcfg["weight_decay"], "name": "connector_w"})
    groups.append({"params": [p for p in conn if p.ndim < 2], "lr": stage["connector_lr"], "weight_decay": 0.0,
                   "name": "connector_b"})
    if stage["lora_lr"]:
        lora = [p for n, p in vlm.llm.named_parameters() if p.requires_grad]
        groups.append({"params": lora, "lr": stage["lora_lr"], "weight_decay": tcfg["weight_decay"], "name": "lora"})
    opt = torch.optim.AdamW(groups)
    lam = stage_lambda(stage["updates"], tcfg["warmup_frac"], tcfg["final_lr_frac"])
    sched = torch.optim.lr_scheduler.LambdaLR(opt, [lam] * len(groups))
    return opt, sched


def save_ckpt(path, vlm, opt, sched, scaler, sampler, prog):
    os.makedirs(path, exist_ok=True)
    torch.save({"trainable": vlm.trainable_state(), "out_scale": vlm.connector.out_scale.item(),
                "optimizer": opt.state_dict(), "scheduler": sched.state_dict(), "scaler": scaler.state_dict(),
                "sampler": sampler.state(), "rng": get_rng_state(), "progress": prog}, os.path.join(path, "state.pt.tmp"))
    os.replace(os.path.join(path, "state.pt.tmp"), os.path.join(path, "state.pt"))


def latest_ckpt(rd):
    cks = [d for d in os.listdir(rd) if d.startswith("ckpt_u") and os.path.exists(os.path.join(rd, d, "state.pt"))]
    return os.path.join(rd, max(cks, key=lambda s: int(s[6:]))) if cks else None


def checks_after_first_backward(vlm, stage_idx):
    g = [p.grad for p in vlm.connector.parameters()]
    conn_gn = float(torch.sqrt(sum((x.float() ** 2).sum() for x in g if x is not None)))
    frozen_ok = all((not p.requires_grad) for n, p in vlm.llm.named_parameters() if "lora_" not in n)
    llm_trainable = sorted({n.split(".")[-1] for n, p in vlm.llm.named_parameters() if p.requires_grad})
    return {"stage": stage_idx + 1, "connector_grad_norm_first_step": conn_gn, "connector_receives_grad": conn_gn > 0,
            "original_llm_params_frozen": frozen_ok, "llm_trainable_param_kinds": llm_trainable,
            "trainable_params": count_trainable(vlm)}


def train(cfg):
    assert torch.cuda.is_available()
    set_seed(cfg["seed"])
    tcfg = cfg["train"]
    rd = run_dir(cfg)
    res_d = results_dir("task2", cfg["run_name"])
    log_path, dev_path = os.path.join(rd, "train_log.jsonl"), os.path.join(rd, "dev_eval.jsonl")
    man = load_manifests(cfg)
    store = FeatureStore(cfg)
    reset_peak_memory()
    vlm = VLM(cfg)
    vlm.train()
    stages = tcfg["stages"]
    bounds = [0]
    for s in stages:
        bounds.append(bounds[-1] + s["updates"])
    total = bounds[-1]

    sampler = CurriculumSampler(man, cfg["seed"])
    scaler = torch.amp.GradScaler("cuda")
    ck = latest_ckpt(rd)
    prog = {"global_update": 0, "mixture_counts": {}, "train_sec": 0.0, "skipped": [], "stage_sec": {},
            "trainable_by_stage": {}, "checks": []}
    blob = None
    if ck:
        blob = torch.load(os.path.join(ck, "state.pt"), map_location="cpu", weights_only=False)
        prog = blob["progress"]
        vlm.connector.out_scale.fill_(blob["out_scale"])
        print("resuming from", ck, "at update", prog["global_update"])
    else:
        for f in (log_path, dev_path):
            if os.path.exists(f):
                os.remove(f)
        cal = calibrate_connector(vlm, store, man["caption_train"], tcfg["calibration_images"], cfg["seed"])
        save_json(cal, os.path.join(res_d, "connector_calibration.json"))
        print("calibration:", cal)
        shape_ok = tuple(store.get([man["caption_train"][0]["image_key"]]).shape) == (1, 196, 768)
        prog["checks"].append({"feature_shape_196x768": shape_ok})

    done_dev = {r["update"] for r in read_jsonl(dev_path)}
    G, mb = tcfg["grad_accum"], tcfg["microbatch"]
    for si, st in enumerate(stages):
        if prog["global_update"] >= bounds[si + 1]:
            if si >= 1 and not vlm.has_lora:
                vlm.enable_lora(cfg["seed"])
            continue
        if si >= 1 and not vlm.has_lora:
            vlm.enable_lora(cfg["seed"])  # LoRA enabled at Stage 2 (zero B -> starts as identity)
        opt, sched = make_opt(vlm, st, tcfg)
        if blob is not None:
            vlm.load_trainable_state(blob["trainable"])
            if prog["global_update"] > bounds[si]:  # mid-stage resume; at a boundary the new stage starts fresh
                opt.load_state_dict(blob["optimizer"])
                sched.load_state_dict(blob["scheduler"])
            scaler.load_state_dict(blob["scaler"])
            sampler.load(blob["sampler"])
            set_rng_state(blob["rng"])
            blob = None
        prog["trainable_by_stage"][st["name"]] = count_trainable(vlm)
        print(f"== stage {si+1} {st['name']}: trainable {count_trainable(vlm):,}")
        if prog["global_update"] == 0 and 0 not in done_dev:
            r = dev_eval(vlm, store, man, 0, 0)
            append_jsonl(dev_path, r)
            done_dev.add(0)
        while prog["global_update"] < bounds[si + 1]:
            t0 = time.perf_counter()
            u = prog["global_update"]
            if si == 0:
                exs, counts = sampler.next_captions(G * mb), {"caption_examples": G * mb}
            else:
                exs, counts = sampler.next_draws(G * mb // 2, st["mixture"])
            for k, v in counts.items():
                prog["mixture_counts"].setdefault(st["name"], {}).setdefault(k, 0)
                prog["mixture_counts"][st["name"]][k] += v
            opt.zero_grad(set_to_none=True)
            n_eff = len(exs)
            loss_tot = 0.0
            for m in range(G):
                ch = exs[m * mb:(m + 1) * mb]
                feats = list(store.get([e["image_key"] for e in ch]))
                with torch.autocast(device_type="cuda", dtype=torch.float16):
                    b = vlm.build(ch, feats, with_target=True)
                per_ex = vlm.per_example_nll(b, include_eot=True)
                loss = per_ex.sum() / n_eff          # example-weighted mean over the effective batch
                scaler.scale(loss).backward()
                loss_tot += float(per_ex.detach().sum())
            if u == bounds[si]:
                scaler.unscale_(opt)
                prog["checks"].append(checks_after_first_backward(vlm, si))
                print(prog["checks"][-1])
            else:
                scaler.unscale_(opt)
            gn = float(torch.nn.utils.clip_grad_norm_(vlm.trainable_parameters(), tcfg["clip_norm"]))
            sb = scaler.get_scale()
            scaler.step(opt)
            scaler.update()
            skipped = scaler.get_scale() < sb
            if skipped:
                prog["skipped"].append(u + 1)
            lrs = {g["name"]: g["lr"] for g in opt.param_groups}
            sched.step()
            torch.cuda.synchronize()
            dt = time.perf_counter() - t0
            prog["train_sec"] += dt
            prog["stage_sec"][st["name"]] = prog["stage_sec"].get(st["name"], 0.0) + dt
            prog["global_update"] = u + 1
            append_jsonl(log_path, {"update": u + 1, "stage": si + 1, "loss": loss_tot / n_eff, "grad_norm": gn,
                                    "skipped": bool(skipped), **{f"lr_{k}": v for k, v in lrs.items()},
                                    "draw_counts": counts, "step_sec": dt})
            if (u + 1) % 50 == 0:
                print(f"u={u+1} stage={si+1} loss={loss_tot/n_eff:.4f} gn={gn:.2f} {dt:.2f}s")
            if (u + 1) in cfg["dev_eval_updates"] and (u + 1) not in done_dev:
                r = dev_eval(vlm, store, man, u + 1, si + 1)
                append_jsonl(dev_path, r)
                done_dev.add(u + 1)
                print({k: round(r[k], 4) for k in ["caption_dev_loss", "grounding_nll_correct", "grounding_swap_penalty",
                                                   "vqa_nll_correct", "vqa_swap_penalty"]})
            if (u + 1) % tcfg["ckpt_every"] == 0 or (u + 1) == bounds[si + 1]:
                save_ckpt(os.path.join(rd, f"ckpt_u{u+1}"), vlm, opt, sched, scaler, sampler, prog)
                for d in os.listdir(rd):  # keep only the latest resumable checkpoint (saves Drive space)
                    if d.startswith("ckpt_u") and d != f"ckpt_u{u+1}":
                        shutil.rmtree(os.path.join(rd, d), ignore_errors=True)
        torch.save({"trainable": vlm.trainable_state(), "out_scale": vlm.connector.out_scale.item()},
                   os.path.join(rd, f"stage{si+1}_weights.pt"))
    pk = peak_memory_mb()
    info = {"run_name": cfg["run_name"], "num_visual_tokens": cfg["num_visual_tokens"],
            "train_sec": prog["train_sec"], "train_hours": prog["train_sec"] / 3600, "stage_sec": prog["stage_sec"],
            "trainable_by_stage": prog["trainable_by_stage"], "mixture_counts": prog["mixture_counts"],
            "skipped_updates": prog["skipped"], "peak_gpu_mem_mb_this_session": pk, "checks": prog["checks"],
            "total_updates": total, "total_example_presentations": total * G * mb,
            "gpu": torch.cuda.get_device_name(0)}
    save_json(info, os.path.join(res_d, "training_info.json"))
    for f in (log_path, dev_path):
        shutil.copy(f, os.path.join(res_d, os.path.basename(f)))
    print(info)
    return vlm


def load_final_model(cfg):
    vlm = VLM(cfg)
    vlm.enable_lora(cfg["seed"])
    blob = torch.load(os.path.join(run_dir(cfg), "stage3_weights.pt"), map_location="cpu")
    vlm.load_trainable_state(blob["trainable"])
    vlm.connector.out_scale.fill_(blob["out_scale"])
    vlm.eval()
    return vlm


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task2.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    train(load_config(a.config, a.set))
