"""Shared helpers: seeding, config loading, I/O, timing, GPU memory, environment capture."""
import json
import os
import platform
import random
import re
import subprocess
import sys
import time
from contextlib import contextmanager

import numpy as np
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------------------- config
def load_config(path, overrides=None):
    """Load a YAML config; `overrides` is a list of 'a.b.c=value' strings (YAML-parsed values)."""
    with open(path) as f:
        cfg = yaml.safe_load(f)
    for ov in overrides or []:
        key, val = ov.split("=", 1)
        node = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = yaml.safe_load(val)
    return cfg


def out_root():
    """All large outputs go under $PA3_OUT (set this to a Google Drive folder in Colab)."""
    return os.environ.get("PA3_OUT", os.path.join(REPO_ROOT, "outputs"))


def out_dir(*parts):
    p = os.path.join(out_root(), *parts)
    os.makedirs(p, exist_ok=True)
    return p


def results_dir(*parts):
    """Small machine-readable results that should be committed to GitHub."""
    p = os.path.join(REPO_ROOT, "results", *parts)
    os.makedirs(p, exist_ok=True)
    return p


# ----------------------------------------------------------------------------- seeding
def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    try:
        import torch
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
    except ImportError:
        pass


def get_rng_state():
    import torch
    st = {"python": random.getstate(), "numpy": np.random.get_state(), "torch": torch.get_rng_state()}
    if torch.cuda.is_available():
        st["cuda"] = torch.cuda.get_rng_state_all()
    return st


def set_rng_state(st):
    import torch
    random.setstate(st["python"])
    np.random.set_state(st["numpy"])
    torch.set_rng_state(st["torch"])
    if "cuda" in st and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(st["cuda"])


# ----------------------------------------------------------------------------- io
def _default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def save_json(obj, path, indent=2):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=indent, default=_default)
    os.replace(tmp, path)


def load_json(path):
    with open(path) as f:
        return json.load(f)


def append_jsonl(path, rec):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, default=_default) + "\n")


def write_jsonl(path, recs):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r, default=_default) + "\n")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def dir_size_bytes(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            total += os.path.getsize(os.path.join(root, fn))
    return total


# ----------------------------------------------------------------------------- timing / memory
class Timer:
    def __init__(self):
        self.elapsed = 0.0

    @contextmanager
    def __call__(self, sync_cuda=True):
        if sync_cuda:
            cuda_sync()
        t0 = time.perf_counter()
        try:
            yield self
        finally:
            if sync_cuda:
                cuda_sync()
            self.elapsed += time.perf_counter() - t0


def cuda_sync():
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.synchronize()
    except ImportError:
        pass


def reset_peak_memory():
    import torch
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()


def peak_memory_mb():
    import torch
    if not torch.cuda.is_available():
        return {"allocated_mb": 0.0, "reserved_mb": 0.0}
    return {"allocated_mb": torch.cuda.max_memory_allocated() / 2**20,
            "reserved_mb": torch.cuda.max_memory_reserved() / 2**20}


# ----------------------------------------------------------------------------- text
def normalize_question(s):
    """Normalization used for GSM8K decontamination: lowercase, non-alphanumerics -> space, collapse spaces."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ----------------------------------------------------------------------------- environment
def record_environment(path):
    info = {"python": sys.version, "platform": platform.platform(), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        import torch
        info["torch"] = torch.__version__
        info["cuda"] = torch.version.cuda
        if torch.cuda.is_available():
            info["gpu"] = torch.cuda.get_device_name(0)
            info["gpu_total_mb"] = torch.cuda.get_device_properties(0).total_memory / 2**20
    except ImportError:
        pass
    for mod in ["transformers", "datasets", "lm_eval", "accelerate", "math_verify", "safetensors", "numpy", "pandas"]:
        try:
            m = __import__(mod)
            info[mod] = getattr(m, "__version__", "unknown")
        except Exception:
            info[mod] = None
    try:
        info["pip_freeze"] = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True,
                                            text=True).stdout.splitlines()
    except Exception:
        pass
    try:
        info["git_commit"] = subprocess.run(["git", "-C", REPO_ROOT, "rev-parse", "HEAD"], capture_output=True,
                                            text=True).stdout.strip()
    except Exception:
        pass
    save_json(info, path)
    return info


def bootstrap_ci(values, n_boot=1000, seed=6304, alpha=0.05):
    """Percentile CI of the mean of a 1-D array of unit-level values (units already paired/differenced)."""
    values = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(values), size=(n_boot, len(values)))
    means = values[idx].mean(axis=1)
    return float(values.mean()), float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--record_env", default=None)
    a = ap.parse_args()
    if a.record_env:
        print(json.dumps({k: v for k, v in record_environment(a.record_env).items() if k != "pip_freeze"}, indent=1))
