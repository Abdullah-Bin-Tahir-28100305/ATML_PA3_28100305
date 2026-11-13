"""Minimal, explicit LoRA / OPLoRA implementation (written for this assignment, no PEFT dependency).

LoRA:   W = W0 + (alpha/r) B A,   A in R^{r x din} (random), B in R^{dout x r} (zeros).
OPLoRA: B~ = B - U_k (U_k^T B),  A~ = A - (A V_k) V_k^T,  dW = (alpha/r) B~ A~
        where W0 = U S V^T and U_k, V_k are the k leading singular bases (computed once in FP32, frozen).
Effective (projected) factors are used in the forward pass (no dense projection matrices are built) and
are exported for inference / merging.
"""
import math
import time

import torch
import torch.nn as nn
import torch.nn.functional as F

LLM_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
ATTN_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj"]


class LoRALinear(nn.Module):
    def __init__(self, base: nn.Linear, r: int, alpha: float, dropout: float = 0.0, protect_k: int = 0,
                 generator: torch.Generator = None, adapter_dtype=torch.float32):
        super().__init__()
        self.base = base
        for p in self.base.parameters():
            p.requires_grad_(False)
        self.r, self.alpha, self.scaling = r, alpha, alpha / r
        din, dout = base.in_features, base.out_features
        dev = base.weight.device
        # PEFT-equivalent init: kaiming_uniform(a=sqrt(5)) == U(-1/sqrt(din), 1/sqrt(din)); done on CPU with an
        # explicit generator so LoRA-16 and OPLoRA-16 receive bit-identical unprojected initial factors.
        bound = 1.0 / math.sqrt(din)
        A0 = (torch.rand(r, din, generator=generator, dtype=torch.float32) * 2 - 1) * bound
        self.lora_A = nn.Parameter(A0.to(device=dev, dtype=adapter_dtype))
        self.lora_B = nn.Parameter(torch.zeros(dout, r, device=dev, dtype=adapter_dtype))
        self.dropout = nn.Dropout(dropout) if dropout > 0 else nn.Identity()
        self.protect_k = protect_k
        if protect_k > 0:
            self.register_buffer("U_k", torch.zeros(dout, protect_k, dtype=torch.float32, device=dev))
            self.register_buffer("V_k", torch.zeros(din, protect_k, dtype=torch.float32, device=dev))

    @torch.no_grad()
    def compute_protection_bases(self):
        W = self.base.weight.detach().float()  # FP32 SVD of the starting weight
        U, S, Vh = torch.linalg.svd(W, full_matrices=False)
        self.U_k.copy_(U[:, : self.protect_k])
        self.V_k.copy_(Vh[: self.protect_k].T)

    def effective_factors(self):
        """Return (A~, B~) in FP32; projection math is kept out of autocast so it stays FP32."""
        with torch.autocast(device_type="cuda", enabled=False):
            A, B = self.lora_A.float(), self.lora_B.float()
            if self.protect_k > 0:
                B = B - self.U_k @ (self.U_k.T @ B)
                A = A - (A @ self.V_k) @ self.V_k.T
        return A, B

    def delta_weight(self):
        A, B = self.effective_factors()
        return self.scaling * (B @ A)

    def forward(self, x):
        out = self.base(x)
        A, B = self.effective_factors()
        h = self.dropout(x)
        if not torch.is_autocast_enabled():  # e.g. pure-FP16 inference: match activation dtype
            A, B = A.to(h.dtype), B.to(h.dtype)
        lora = F.linear(F.linear(h, A), B) * self.scaling  # under autocast these run in FP16
        return out + lora.to(out.dtype)


def inject_lora(model, targets, r, alpha, dropout, protect_k=0, seed=6304):
    """Replace every nn.Linear whose attribute name is in `targets` with a LoRALinear. Returns list of names."""
    gen = torch.Generator().manual_seed(seed)
    replaced = []
    for mod_name, mod in list(model.named_modules()):
        for child_name, child in list(mod.named_children()):
            if child_name in targets and isinstance(child, nn.Linear):
                setattr(mod, child_name, LoRALinear(child, r, alpha, dropout, protect_k, generator=gen))
                replaced.append(f"{mod_name}.{child_name}" if mod_name else child_name)
    return replaced


def lora_modules(model):
    return {n: m for n, m in model.named_modules() if isinstance(m, LoRALinear)}


def compute_all_protection_bases(model):
    t0 = time.perf_counter()
    nbytes = 0
    for m in lora_modules(model).values():
        if m.protect_k > 0:
            m.compute_protection_bases()
            nbytes += m.U_k.numel() * 4 + m.V_k.numel() * 4
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    return {"basis_preprocessing_sec": time.perf_counter() - t0, "basis_storage_bytes": nbytes}


@torch.no_grad()
def export_effective_factors(model):
    """State dict of projected (effective) factors, ready for inference without bases."""
    sd = {}
    for name, m in lora_modules(model).items():
        A, B = m.effective_factors()
        sd[f"{name}.lora_A"] = A.detach().float().cpu().contiguous()
        sd[f"{name}.lora_B"] = B.detach().float().cpu().contiguous()
        sd[f"{name}.scaling"] = torch.tensor(m.scaling)
    return sd


@torch.no_grad()
def export_bases(model):
    sd = {}
    for name, m in lora_modules(model).items():
        if m.protect_k > 0:
            sd[f"{name}.U_k"] = m.U_k.detach().cpu().contiguous()
            sd[f"{name}.V_k"] = m.V_k.detach().cpu().contiguous()
    return sd


def adapter_names_from_sd(sd):
    return sorted({k.rsplit(".", 1)[0] for k in sd if k.endswith(".lora_A")})


@torch.no_grad()
def merge_factors_into_model(model, sd):
    """W <- W0 + scaling * B A (FP32 math, cast back to the weight dtype). `model` must be a fresh backbone."""
    modules = dict(model.named_modules())
    for name in adapter_names_from_sd(sd):
        lin = modules[name]
        W = lin.weight
        A = sd[f"{name}.lora_A"].to(W.device, torch.float32)
        B = sd[f"{name}.lora_B"].to(W.device, torch.float32)
        s = float(sd[f"{name}.scaling"])
        W.copy_((W.float() + s * (B @ A)).to(W.dtype))
    return model


@torch.no_grad()
def attach_unmerged_factors(model, sd, dropout=0.0):
    """Wrap a fresh backbone with LoRALinear modules holding already-projected factors (protect_k=0)."""
    modules = dict(model.named_modules())
    for name in adapter_names_from_sd(sd):
        parent_name, child = name.rsplit(".", 1) if "." in name else ("", name)
        parent = modules[parent_name] if parent_name else model
        base = getattr(parent, child)
        A, B = sd[f"{name}.lora_A"], sd[f"{name}.lora_B"]
        s = float(sd[f"{name}.scaling"])
        m = LoRALinear(base, A.shape[0], s * A.shape[0], dropout, 0, generator=torch.Generator().manual_seed(0))
        m.lora_A.data.copy_(A.to(m.lora_A.device))
        m.lora_B.data.copy_(B.to(m.lora_B.device))
        setattr(parent, child, m)
    return model


def count_trainable(model):
    seen, n = set(), 0
    for p in model.parameters():
        if p.requires_grad and id(p) not in seen:
            seen.add(id(p))
            n += p.numel()
    return n


def count_all_unique(model):
    seen, n = set(), 0
    for p in model.parameters():
        if id(p) not in seen:
            seen.add(id(p))
            n += p.numel()
    return n
