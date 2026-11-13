"""Cache frozen SigLIP patch features (196 x 768, FP16) for every needed image.

* Deterministic preprocessing: SiglipImageProcessor (224x224 resize, rescale, normalize), RGB conversion.
* Uses `last_hidden_state` of SiglipVisionModel, which ALREADY includes the final `post_layernorm`; we verify
  numerically that post_layernorm(hidden_states[-1]) == last_hidden_state, i.e. the final LN is applied exactly
  once and never re-applied.
* Patches are kept in their 14x14 row-major spatial order (Conv2d patch embedding -> flatten(2)).
* The vision encoder is deleted from GPU memory afterwards. Caching time/memory are logged separately.

Usage:  python -m task2_vlm.cache_features
"""
import argparse
import os
import time

import numpy as np
import torch

from common.utils import load_config, load_json, peak_memory_mb, reset_peak_memory, results_dir, save_json
from task2_vlm.prepare_data import droot


def feature_paths(cfg):
    d = droot(cfg, "features")
    return os.path.join(d, "siglip_feats_fp16.npy"), os.path.join(d, "siglip_index.json")


@torch.no_grad()
def cache(cfg, batch_size=64):
    from PIL import Image
    from transformers import SiglipImageProcessor, SiglipVisionModel
    fpath, ipath = feature_paths(cfg)
    if os.path.exists(ipath):
        print("features already cached:", fpath)
        return
    needed = load_json(os.path.join(droot(cfg, "manifests"), "needed_images.json"))
    keys = sorted(needed)
    proc = SiglipImageProcessor.from_pretrained(cfg["vision_id"])
    reset_peak_memory()
    t0 = time.perf_counter()
    model = SiglipVisionModel.from_pretrained(cfg["vision_id"], torch_dtype=torch.float32).cuda().eval()
    load_sec = time.perf_counter() - t0
    feats = np.lib.format.open_memmap(fpath + ".tmp.npy", mode="w+", dtype=np.float16, shape=(len(keys), 196, 768))
    max_ln_err = 0.0
    t1 = time.perf_counter()
    for s in range(0, len(keys), batch_size):
        ks = keys[s:s + batch_size]
        ims = [Image.open(droot(cfg, "images", k.split(":")[0], needed[k])).convert("RGB") for k in ks]
        px = proc(images=ims, return_tensors="pt")["pixel_values"].cuda()
        out = model(pixel_values=px, output_hidden_states=True)
        lhs = out.last_hidden_state
        assert lhs.shape[1:] == (196, 768), lhs.shape
        if s == 0 or s % (batch_size * 20) == 0:
            ref = model.vision_model.post_layernorm(out.hidden_states[-1])
            max_ln_err = max(max_ln_err, float((ref - lhs).abs().max()))
        feats[s:s + len(ks)] = lhs.half().cpu().numpy()
    feats.flush()
    del feats
    os.replace(fpath + ".tmp.npy", fpath)
    enc_sec = time.perf_counter() - t1
    pk = peak_memory_mb()
    del model
    torch.cuda.empty_cache()
    assert max_ln_err < 1e-3, f"final LN check failed ({max_ln_err})"
    save_json({k: i for i, k in enumerate(keys)}, ipath, indent=None)
    info = {"n_images": len(keys), "feature_shape": [196, 768], "dtype": "float16", "model_load_sec": load_sec,
            "encode_sec": enc_sec, "sec_per_image": enc_sec / len(keys), "peak_gpu_mem_mb": pk,
            "final_layernorm_applied_once_max_abs_err": max_ln_err, "file_bytes": os.path.getsize(fpath),
            "gpu": torch.cuda.get_device_name(0)}
    save_json(info, os.path.join(results_dir("task2", "resources"), "feature_caching.json"))
    print(info)


class FeatureStore:
    def __init__(self, cfg):
        fpath, ipath = feature_paths(cfg)
        local = os.environ.get("PA3_FEAT_LOCAL")  # optional fast local copy (Drive FUSE is slow for random reads)
        if local and os.path.exists(os.path.join(local, os.path.basename(fpath))):
            fpath = os.path.join(local, os.path.basename(fpath))
        self.arr = np.load(fpath, mmap_mode="r")
        self.index = load_json(ipath)

    def get(self, keys):
        return torch.from_numpy(np.stack([np.asarray(self.arr[self.index[k]]) for k in keys]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task2.yaml")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    cache(load_config(a.config, a.set))
