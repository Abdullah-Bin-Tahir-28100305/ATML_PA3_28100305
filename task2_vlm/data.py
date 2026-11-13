"""Task 2 example pools and the seeded curriculum sampler (state is saved/restored for exact resumption)."""
import os

import numpy as np

from common.utils import load_json
from task2_vlm.prepare_data import droot


def load_manifests(cfg):
    d = droot(cfg, "manifests")
    names = ["caption_train", "caption_dev", "presence_pool", "counting_pool", "vqa_train", "grounding_dev",
             "grounding_test", "vqa_dev", "vqa_test"]
    return {n: load_json(os.path.join(d, f"{n}.json")) for n in names}


def caption_ex(c):
    return {"task": "caption", "question": None, "answer": c["caption"], "image_key": c["image_key"]}


def pair_exs(p):
    return [{"task": "short", "question": it["question"], "answer": it["answer"], "image_key": it["image_key"],
             "kind": p["kind"]} for it in p["items"]]


class CurriculumSampler:
    """Stage 1: two passes over the 18,000 caption examples, each pass a fresh seeded permutation.
    Stages 2-3: each draw picks a component by the stage probabilities (seeded RNG, no exact quotas) and
    contributes TWO examples: a complete question pair, or two independently drawn caption examples (replay)."""

    def __init__(self, man, seed):
        self.man = man
        self.rng = np.random.default_rng(seed)
        self.cap_perm, self.cap_pos = None, 0
        self.pools = {"presence": man["presence_pool"], "counting": man["counting_pool"], "vqa": man["vqa_train"]}

    def state(self):
        return {"rng": self.rng.bit_generator.state, "cap_perm": None if self.cap_perm is None else self.cap_perm.tolist(),
                "cap_pos": self.cap_pos}

    def load(self, st):
        self.rng.bit_generator.state = st["rng"]
        self.cap_perm = None if st["cap_perm"] is None else np.array(st["cap_perm"])
        self.cap_pos = st["cap_pos"]

    def next_captions(self, n):
        caps = self.man["caption_train"]
        out = []
        while len(out) < n:
            if self.cap_perm is None or self.cap_pos >= len(caps):
                self.cap_perm, self.cap_pos = self.rng.permutation(len(caps)), 0
            out.append(caption_ex(caps[self.cap_perm[self.cap_pos]]))
            self.cap_pos += 1
        return out

    def next_draws(self, n_draws, mixture):
        comps, probs = list(mixture.keys()), np.array(list(mixture.values()), dtype=float)
        probs /= probs.sum()
        exs, counts = [], {}
        for _ in range(n_draws):
            c = comps[self.rng.choice(len(comps), p=probs)]
            counts[c] = counts.get(c, 0) + 1
            if c == "caption":
                caps = self.man["caption_train"]
                exs += [caption_ex(caps[self.rng.integers(len(caps))]) for _ in range(2)]
            else:
                pool = self.pools[c]
                exs += pair_exs(pool[self.rng.integers(len(pool))])
        return exs, counts
