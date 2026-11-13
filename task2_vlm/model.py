"""SigLIP features -> 2-layer MLP connector -> Qwen3-0.6B (frozen FP16, optional LoRA on attention projections).

Sequence layout (Qwen chat structure preserved; visual tokens inserted in the user message BEFORE the question):
  <|im_start|>user\n [V_1 ... V_T] \n <question tokens (<=80)> [\nAnswer the question using a single word or phrase.]
  <|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n <target tokens (<=64 caption / <=16 answer)> <|im_end|>
The no-image condition drops V_1..V_T only (text instructions unchanged); masks and positions are rebuilt.
Loss: causal CE on target tokens + <|im_end|>, averaged within each example, then across the effective batch.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F

from common.lora import ATTN_TARGETS, inject_lora


class Connector(nn.Module):
    def __init__(self, din=768, dmodel=1024, pool2x2=False):
        super().__init__()
        self.fc1 = nn.Linear(din, dmodel)
        self.act = nn.GELU()
        self.fc2 = nn.Linear(dmodel, dmodel)
        self.pool2x2 = pool2x2
        self.register_buffer("out_scale", torch.tensor(1.0))  # fixed calibration scalar (see calibrate())

    def forward(self, f):
        if self.pool2x2:  # 14x14 -> 7x7 by averaging each 2x2 block, before the connector
            B = f.shape[0]
            f = f.view(B, 14, 14, -1).permute(0, 3, 1, 2)
            f = F.avg_pool2d(f, 2).flatten(2).transpose(1, 2)
        return self.fc2(self.act(self.fc1(f))) * self.out_scale


class VLM(nn.Module):
    def __init__(self, cfg, device="cuda"):
        super().__init__()
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.cfg = cfg
        self.device_ = device
        self.tok = AutoTokenizer.from_pretrained(cfg["llm_id"])
        self.llm = AutoModelForCausalLM.from_pretrained(cfg["llm_id"], torch_dtype=torch.float16,
                                                        attn_implementation="sdpa").to(device)
        for p in self.llm.parameters():
            p.requires_grad_(False)
        self.llm.config.use_cache = False
        self.llm.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        self.connector = Connector(pool2x2=cfg["num_visual_tokens"] == 49).to(device)  # FP32 trainable
        self.embed = self.llm.get_input_embeddings()
        s = self.tok.apply_chat_template([{"role": "user", "content": "<IMG>\n<Q>"}], tokenize=False,
                                         add_generation_prompt=True, enable_thinking=False)
        pre, rest = s.split("<IMG>")
        mid, post = rest.split("<Q>")
        enc = lambda t: self.tok(t, add_special_tokens=False).input_ids
        self.pre_ids, self.mid_ids, self.post_ids = enc(pre), enc(mid), enc(post)
        self.im_end = self.tok.convert_tokens_to_ids("<|im_end|>")
        self.suffix_ids = enc("\n" + cfg["prompts"]["short_answer_suffix"])
        self.caption_q_ids = enc(cfg["prompts"]["caption"])
        self.has_lora = False

    # ------------------------------------------------------------------ setup
    def enable_lora(self, seed):
        L = self.cfg["train"]["lora"]
        inject_lora(self.llm, L.get("targets", ATTN_TARGETS), L["r"], L["alpha"], L["dropout"], 0, seed=seed)
        self.has_lora = True

    def trainable_state(self):
        sd = {f"connector.{k}": v for k, v in self.connector.state_dict().items()}
        for n, p in self.llm.named_parameters():
            if p.requires_grad:
                sd[f"llm.{n}"] = p.detach()
        return sd

    def load_trainable_state(self, sd):
        self.connector.load_state_dict({k[10:]: v for k, v in sd.items() if k.startswith("connector.")})
        named = dict(self.llm.named_parameters())
        with torch.no_grad():
            for k, v in sd.items():
                if k.startswith("llm."):
                    named[k[4:]].copy_(v.to(named[k[4:]].device))

    def trainable_parameters(self):
        return [p for p in self.parameters() if p.requires_grad]

    # ------------------------------------------------------------------ tokenization
    def encode(self, ex):
        """ex: {'task': 'caption'|'short', 'question': str|None, 'answer': str|None}."""
        caps = self.cfg["caps"]
        if ex["task"] == "caption":
            q = self.caption_q_ids[: caps["question"]]
            tcap = caps["caption_target"]
        else:
            q = self.tok(ex["question"], add_special_tokens=False).input_ids[: caps["question"]] + self.suffix_ids
            tcap = caps["answer_target"]
        tgt = None
        if ex.get("answer") is not None:
            tgt = self.tok(ex["answer"], add_special_tokens=False).input_ids[:tcap] + [self.im_end]
        return self.mid_ids + q + self.post_ids, tgt

    def build(self, examples, feats, with_target=True, left_pad=False):
        """examples: list of dicts; feats: list of (196,768) tensors or None (no image). Returns batch dict."""
        dev = self.device_
        has = [f is not None for f in feats]
        vis = None
        if any(has):
            F_ = torch.stack([f for f in feats if f is not None]).to(dev).float()
            vis = self.connector(F_)
        vi = 0
        embs, labs, eots = [], [], []
        for ex, h in zip(examples, has):
            rest, tgt = self.encode(ex)
            pre = torch.tensor(self.pre_ids, device=dev)
            parts = [self.embed(pre)]
            n_ctx = len(self.pre_ids)
            if h:
                parts.append(vis[vi].to(parts[0].dtype) if not torch.is_autocast_enabled() else vis[vi])
                n_ctx += vis.shape[1]
                vi += 1
            ids_rest = rest + (tgt if (with_target and tgt) else [])
            parts.append(self.embed(torch.tensor(ids_rest, device=dev)))
            n_ctx += len(rest)
            e = torch.cat([p.to(parts[0].dtype) for p in parts], 0)
            lab = torch.full((e.shape[0],), -100, dtype=torch.long, device=dev)
            eot = torch.zeros(e.shape[0], dtype=torch.bool, device=dev)
            if with_target and tgt:
                lab[n_ctx:] = torch.tensor(tgt, device=dev)
                eot[-1] = True
            embs.append(e)
            labs.append(lab)
            eots.append(eot)
        L = max(e.shape[0] for e in embs)
        B, D = len(embs), embs[0].shape[1]
        E = torch.zeros(B, L, D, dtype=embs[0].dtype, device=dev)
        lab = torch.full((B, L), -100, dtype=torch.long, device=dev)
        eot = torch.zeros(B, L, dtype=torch.bool, device=dev)
        am = torch.zeros(B, L, dtype=torch.long, device=dev)
        for i, (e, l, t) in enumerate(zip(embs, labs, eots)):
            n = e.shape[0]
            sl = slice(L - n, L) if left_pad else slice(0, n)
            E[i, sl], lab[i, sl], eot[i, sl], am[i, sl] = e, l, t, 1
        pos = (am.cumsum(-1) - 1).clamp(min=0)
        return {"inputs_embeds": E, "labels": lab, "eot": eot, "attention_mask": am, "position_ids": pos}

    # ------------------------------------------------------------------ objectives
    def per_example_nll(self, batch, include_eot=True):
        """Mean token NLL per example over supervised positions (optionally excluding <|im_end|>)."""
        with torch.autocast(device_type="cuda", dtype=torch.float16):
            h = self.llm.model(inputs_embeds=batch["inputs_embeds"], attention_mask=batch["attention_mask"],
                               position_ids=batch["position_ids"]).last_hidden_state[:, :-1]
            tgt = batch["labels"][:, 1:]
            mask = tgt != -100
            if not include_eot:
                mask &= ~batch["eot"][:, 1:]
            logits = self.llm.lm_head(h[mask])
        ce = F.cross_entropy(logits.float(), tgt[mask], reduction="none")
        B = tgt.shape[0]
        row = torch.arange(B, device=tgt.device)[:, None].expand_as(tgt)[mask]
        tot = torch.zeros(B, device=tgt.device).scatter_add_(0, row, ce)
        cnt = torch.zeros(B, device=tgt.device).scatter_add_(0, row, torch.ones_like(ce))
        return tot / cnt.clamp(min=1)

    # ------------------------------------------------------------------ greedy decoding with KV cache
    @torch.no_grad()
    def generate(self, examples, feats, max_new_tokens=16):
        with torch.autocast(device_type="cuda", dtype=torch.float16):
            b = self.build(examples, feats, with_target=False, left_pad=True)
            am, pos = b["attention_mask"], b["position_ids"]
            out = self.llm(inputs_embeds=b["inputs_embeds"], attention_mask=am, position_ids=pos, use_cache=True)
            past = out.past_key_values
            nxt = out.logits[:, -1].argmax(-1)
            B = nxt.shape[0]
            gen = [[] for _ in range(B)]
            done = torch.zeros(B, dtype=torch.bool, device=nxt.device)
            stop = {self.im_end, self.tok.eos_token_id}
            for step in range(max_new_tokens):
                for i in range(B):
                    if not done[i]:
                        t = int(nxt[i])
                        if t in stop:
                            done[i] = True
                        else:
                            gen[i].append(t)
                if bool(done.all()) or step == max_new_tokens - 1:
                    break
                am = torch.cat([am, torch.ones(B, 1, dtype=am.dtype, device=am.device)], 1)
                pos = pos[:, -1:] + 1
                out = self.llm(inputs_embeds=self.embed(nxt[:, None]), attention_mask=am, position_ids=pos,
                               past_key_values=past, use_cache=True)
                past = out.past_key_values
                nxt = out.logits[:, -1].argmax(-1)
        return [self.tok.decode(g, skip_special_tokens=True).strip() for g in gen]


@torch.no_grad()
def calibrate_connector(vlm, store, caption_examples, n_images, seed):
    """Rule: fixed scalar s = mean per-token L2 norm of Qwen input embeddings of TRAINING caption/question text
    tokens / mean per-token L2 norm of the freshly initialized connector outputs on n TRAINING images.
    Applied as a non-trainable multiplier on the connector output."""
    import numpy as np
    rng = np.random.default_rng(seed)
    keys = sorted({e["image_key"] for e in caption_examples})
    pick = [keys[i] for i in rng.permutation(len(keys))[:n_images]]
    vis_norms = []
    for s in range(0, len(pick), 64):
        f = store.get(pick[s:s + 64]).to(vlm.device_).float()
        vis_norms.append(vlm.connector(f).norm(dim=-1).flatten().cpu())
    vis = float(torch.cat(vis_norms).mean())
    texts = [caption_examples[i]["caption"] for i in rng.permutation(len(caption_examples))[:2000]]
    ids = torch.tensor([t for x in texts for t in vlm.tok(x, add_special_tokens=False).input_ids], device=vlm.device_)
    txt = float(vlm.embed(ids).float().norm(dim=-1).mean())
    scale = txt / vis
    vlm.connector.out_scale.fill_(scale)
    return {"rule": "s = mean||text-token embedding|| (2000 training captions) / mean||connector(F)|| at init "
                    f"({n_images} training images); fixed, non-trainable", "text_token_norm": txt,
            "connector_token_norm_at_init": vis, "scale": scale}
