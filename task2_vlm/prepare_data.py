"""Task 2 data preparation (COCO 2014 captions + instances, VQAv2 complementary pairs).

Produces JSON manifests under $PA3_OUT/data/task2/manifests and the selected image/question/pair IDs under
results/task2/splits. Only the images actually needed are downloaded (per-image COCO URLs), not the 19 GB zips.

Splits: train2014 -> training; val2014 -> development + final test. All six evaluation sets (caption dev,
grounding dev/test, VQA dev/test) are mutually image-disjoint and disjoint from training/replay.

Annotation-derived grounding (generated programmatically from COCO instances; crowd boxes never counted):
  presence: "Does this image contain <obj>?"  positive image has >=1 instance of the category, negative image has
            no annotation of it at all -> answers "yes"/"no".
  counting: "How many <objs> can you see?"     two images with different annotated counts in 1..4 (no crowd box
            for that category) -> answers "1".."4" (digits; VQA normalization maps number words to digits).
Natural VQAv2: complementary pairs whose normalized consensus answers differ, each with >= 8/10 agreement and the
same answer_type; target = most frequent ORIGINAL answer string within the consensus group; all 10 answers kept.

Usage:  python -m task2_vlm.prepare_data [--inspect]
"""
import argparse
import io
import os
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from common.utils import load_config, load_json, out_root, results_dir, save_json
from task2_vlm.vqa_eval import normalize_answer

PLURAL = {"person": "people", "sheep": "sheep", "skis": "skis", "scissors": "scissors", "mouse": "mice",
          "knife": "knives", "bus": "buses", "couch": "couches", "sandwich": "sandwiches", "wine glass": "wine glasses",
          "bench": "benches", "toothbrush": "toothbrushes", "broccoli": "broccoli", "skateboard": "skateboards",
          "tv": "TVs", "hair drier": "hair driers"}
MASS = {"skis", "scissors", "broccoli"}  # no article in presence questions


def droot(cfg, *p):
    d = os.path.join(out_root(), cfg["data"]["root"], *p)
    os.makedirs(d if not os.path.splitext(d)[1] else os.path.dirname(d), exist_ok=True)
    return d


def obj_phrase(name):
    if name in MASS:
        return name
    return ("an " if name[0] in "aeiou" else "a ") + name


def plural(name):
    return PLURAL.get(name, name + ("es" if name.endswith(("s", "sh", "ch", "x")) else "s"))


# ----------------------------------------------------------------------------- downloads
def fetch_zip(url, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    marker = os.path.join(dest_dir, "." + os.path.basename(url) + ".done")
    if os.path.exists(marker):
        return
    print("downloading", url)
    r = requests.get(url, timeout=600)
    r.raise_for_status()
    zipfile.ZipFile(io.BytesIO(r.content)).extractall(dest_dir)
    open(marker, "w").close()


def find_file(root, name):
    for dp, _, fns in os.walk(root):
        if name in fns:
            return os.path.join(dp, name)
    raise FileNotFoundError(name)


def download_images(cfg, needed):
    """needed: dict key 'split:image_id' -> file_name."""
    def one(item):
        key, fn = item
        split = key.split(":")[0]
        path = droot(cfg, "images", split, fn)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return 0
        url = cfg["data"]["coco_image_url"].format(split=split, file_name=fn)
        for _ in range(4):
            try:
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                with open(path + ".tmp", "wb") as f:
                    f.write(r.content)
                os.replace(path + ".tmp", path)
                return 1
            except Exception as e:  # noqa
                err = e
        raise RuntimeError(f"failed {url}: {err}")
    with ThreadPoolExecutor(cfg["data"]["download_workers"]) as ex:
        n = sum(ex.map(one, sorted(needed.items())))
    print(f"downloaded {n} new images ({len(needed)} needed)")


# ----------------------------------------------------------------------------- COCO indexing
def index_instances(inst, min_area):
    imgs = {im["id"]: im for im in inst["images"]}
    cats = {c["id"]: c["name"] for c in inst["categories"]}
    per = defaultdict(dict)
    for a in inst["annotations"]:
        d = per[a["image_id"]].setdefault(a["category_id"], {"n": 0, "crowd": 0, "max_area": 0.0})
        if a["iscrowd"]:
            d["crowd"] += 1
        else:
            im = imgs[a["image_id"]]
            d["n"] += 1
            d["max_area"] = max(d["max_area"], a["area"] / (im["width"] * im["height"]))
    return imgs, cats, per


def presence_candidates(images, cats, per, min_area):
    pos, neg = defaultdict(list), defaultdict(list)
    for i in sorted(images):
        for c in cats:
            d = per.get(i, {}).get(c)
            if d is None:
                neg[c].append(i)
            elif d["n"] >= 1 and d["max_area"] >= min_area:
                pos[c].append(i)
    return pos, neg


def counting_candidates(images, cats, per):
    bins = defaultdict(lambda: defaultdict(list))
    for i in sorted(images):
        for c, d in per.get(i, {}).items():
            if d["crowd"] == 0 and 1 <= d["n"] <= 4:
                bins[c][d["n"]].append(i)
    return bins


def make_presence_pairs(pos, neg, cats, split, rng, n_max, used=None):
    cand = [(c, i) for c in sorted(pos) for i in pos[c]]
    order = rng.permutation(len(cand))
    pairs = []
    for k in order:
        if len(pairs) >= n_max:
            break
        c, i = cand[k]
        if used is not None and f"{split}:{i}" in used:
            continue
        negs = neg[c] if used is None else [j for j in neg[c] if f"{split}:{j}" not in used and j != i]
        if not negs:
            continue
        j = negs[rng.integers(len(negs))]
        if used is not None:
            used.update([f"{split}:{i}", f"{split}:{j}"])
        items = [{"image_key": f"{split}:{i}", "answer": "yes"}, {"image_key": f"{split}:{j}", "answer": "no"}]
        if rng.random() < 0.5:
            items = items[::-1]
        q = f"Does this image contain {obj_phrase(cats[c])}?"
        for it in items:
            it["question"] = q
        pairs.append({"pair_id": f"pres_{split}_{c}_{i}_{j}", "kind": "presence", "category": cats[c],
                      "question": q, "items": items})
    return pairs


def make_counting_pairs(bins, cats, split, rng, n_max, used=None):
    cand = [(c, n, i) for c in sorted(bins) for n in sorted(bins[c]) for i in bins[c][n]]
    order = rng.permutation(len(cand))
    pairs = []
    for k in order:
        if len(pairs) >= n_max:
            break
        c, n, i = cand[k]
        if used is not None and f"{split}:{i}" in used:
            continue
        others = [(m, j) for m in sorted(bins[c]) if m != n for j in bins[c][m]
                  if used is None or (f"{split}:{j}" not in used and j != i)]
        if not others:
            continue
        m, j = others[rng.integers(len(others))]
        if used is not None:
            used.update([f"{split}:{i}", f"{split}:{j}"])
        q = f"How many {plural(cats[c])} can you see?"
        items = [{"image_key": f"{split}:{i}", "answer": str(n), "question": q},
                 {"image_key": f"{split}:{j}", "answer": str(m), "question": q}]
        if rng.random() < 0.5:
            items = items[::-1]
        pairs.append({"pair_id": f"count_{split}_{c}_{i}_{j}", "kind": "counting", "category": cats[c],
                      "question": q, "items": items})
    return pairs


# ----------------------------------------------------------------------------- VQAv2
def vqa_eligible_pairs(pairs, questions, annotations, split, min_agree):
    qs = {q["question_id"]: q for q in questions}
    an = {a["question_id"]: a for a in annotations}
    out = []
    for q1, q2 in pairs:
        items, ok = [], True
        for qid in (q1, q2):
            a, q = an[qid], qs[qid]
            raw = [x["answer"] for x in a["answers"]]
            norm = [normalize_answer(x) for x in raw]
            cons, cnt = Counter(norm).most_common(1)[0]
            if cnt < min_agree:
                ok = False
                break
            raws = Counter(r.strip() for r, n in zip(raw, norm) if n == cons)
            target = sorted(raws.items(), key=lambda t: (-t[1], t[0]))[0][0]
            items.append({"question_id": qid, "image_key": f"{split}:{q['image_id']}", "question": q["question"],
                          "answers": raw, "consensus": cons, "agreement": cnt, "answer": target,
                          "answer_type": a["answer_type"]})
        if not ok or items[0]["consensus"] == items[1]["consensus"]:
            continue
        if items[0]["answer_type"] != items[1]["answer_type"] or items[0]["image_key"] == items[1]["image_key"]:
            continue
        out.append({"pair_id": f"vqa_{q1}_{q2}", "kind": items[0]["answer_type"], "question": items[0]["question"],
                    "items": items})
    return sorted(out, key=lambda p: p["pair_id"])


def pick_pairs(pool, counts, rng, used=None):
    sel = []
    for kind, n in counts.items():
        cands = [p for p in pool if p["kind"] == kind]
        got = 0
        for k in rng.permutation(len(cands)):
            if got >= n:
                break
            p = cands[k]
            keys = [it["image_key"] for it in p["items"]]
            if used is not None and any(x in used for x in keys):
                continue
            if used is not None:
                used.update(keys)
            sel.append(p)
            got += 1
        assert got == n, f"only {got}/{n} pairs of kind {kind}"
    return sel


# ----------------------------------------------------------------------------- main
def prepare(cfg):
    D = cfg["data"]
    rng = np.random.default_rng(cfg["seed"])
    raw = droot(cfg, "raw")
    fetch_zip(D["coco_annotations_url"], raw)
    for k, u in D["vqa_urls"].items():
        fetch_zip(u, raw)
    inst = {s: load_json(find_file(raw, f"instances_{s}2014.json")) for s in ["train", "val"]}
    caps = {s: load_json(find_file(raw, f"captions_{s}2014.json")) for s in ["train", "val"]}
    vq = {s: load_json(find_file(raw, f"v2_OpenEnded_mscoco_{s}2014_questions.json"))["questions"] for s in ["train", "val"]}
    va = {s: load_json(find_file(raw, f"v2_mscoco_{s}2014_annotations.json"))["annotations"] for s in ["train", "val"]}
    vp = {s: load_json(find_file(raw, f"v2_mscoco_{s}2014_complementary_pairs.json")) for s in ["train", "val"]}

    idx = {s: index_instances(inst[s], D["min_object_area_frac"]) for s in ["train", "val"]}
    file_of = {f"{s}:{i}": im["file_name"] for s in ["train", "val"] for i, im in idx[s][0].items()}
    man = {}

    # ---------------- evaluation sets (val2014), mutually image-disjoint
    used = set()
    vqa_val = vqa_eligible_pairs(vp["val"], vq["val"], va["val"], "val", D["vqa_min_agreement"])
    man["vqa_test"] = pick_pairs(vqa_val, D["vqa_test"], rng, used)
    man["vqa_dev"] = pick_pairs(vqa_val, D["vqa_dev"], rng, used)
    imgs_v, cats_v, per_v = idx["val"]
    pos_v, neg_v = presence_candidates(imgs_v, cats_v, per_v, D["min_object_area_frac"])
    bins_v = counting_candidates(imgs_v, cats_v, per_v)
    for split_name in ["grounding_test", "grounding_dev"]:
        c = D[split_name]
        man[split_name] = make_presence_pairs(pos_v, neg_v, cats_v, "val", rng, c["presence"], used) + \
            make_counting_pairs(bins_v, cats_v, "val", rng, c["counting"], used)
        assert len(man[split_name]) == c["presence"] + c["counting"]
    # caption dev: 96 unused val images, one caption each (lowest annotation id)
    cap_by_img = defaultdict(list)
    for a in sorted(caps["val"]["annotations"], key=lambda a: a["id"]):
        cap_by_img[a["image_id"]].append(a)
    val_ids = sorted(cap_by_img)
    man["caption_dev"] = []
    for k in rng.permutation(len(val_ids)):
        i = val_ids[k]
        key = f"val:{i}"
        if key in used:
            continue
        used.add(key)
        a = cap_by_img[i][0]
        man["caption_dev"].append({"image_key": key, "caption": a["caption"].strip(), "caption_id": a["id"]})
        if len(man["caption_dev"]) == D["caption_dev_images"]:
            break

    # ---------------- training (train2014)
    cap_tr = defaultdict(list)
    for a in sorted(caps["train"]["annotations"], key=lambda a: a["id"]):
        cap_tr[a["image_id"]].append(a)
    tr_ids = sorted(i for i in cap_tr if len(cap_tr[i]) >= D["captions_per_train_image"])
    sel = sorted(int(tr_ids[k]) for k in rng.permutation(len(tr_ids))[: D["caption_train_images"]])
    man["caption_train"] = [{"image_key": f"train:{i}", "caption": a["caption"].strip(), "caption_id": a["id"]}
                            for i in sel for a in cap_tr[i][: D["captions_per_train_image"]]]
    imgs_t, cats_t, per_t = idx["train"]
    rest = sorted(set(imgs_t) - set(sel))
    extra = sorted(int(rest[k]) for k in rng.permutation(len(rest))[: D["extra_grounding_train_images"]])
    pool_imgs = {i: imgs_t[i] for i in sorted(set(sel) | set(extra))}
    pos_t, neg_t = presence_candidates(pool_imgs, cats_t, per_t, D["min_object_area_frac"])
    man["presence_pool"] = make_presence_pairs(pos_t, neg_t, cats_t, "train", rng, D["presence_pool_max"])
    man["counting_pool"] = make_counting_pairs(counting_candidates(pool_imgs, cats_t, per_t), cats_t, "train", rng,
                                               D["counting_pool_max"])
    vqa_tr = vqa_eligible_pairs(vp["train"], vq["train"], va["train"], "train", D["vqa_min_agreement"])
    man["vqa_train"] = pick_pairs(vqa_tr, D["vqa_train"], rng, None)

    # ---------------- isolation checks
    def keys_of(name):
        m = man[name]
        if name.startswith("caption"):
            return {x["image_key"] for x in m}
        return {it["image_key"] for p in m for it in p["items"]}
    eval_sets = ["caption_dev", "grounding_dev", "grounding_test", "vqa_dev", "vqa_test"]
    train_sets = ["caption_train", "presence_pool", "counting_pool", "vqa_train"]
    train_keys = set().union(*[keys_of(n) for n in train_sets])
    checks = {}
    for i, a in enumerate(eval_sets):
        assert not (keys_of(a) & train_keys), a
        assert all(k.startswith("val:") for k in keys_of(a))
        for b in eval_sets[i + 1:]:
            assert not (keys_of(a) & keys_of(b)), (a, b)
    assert all(k.startswith("train:") for k in train_keys)
    for n in eval_sets + train_sets:
        checks[n] = {"n_items": len(man[n]), "n_images": len(keys_of(n))}
        if n not in ("caption_train", "caption_dev"):
            checks[n]["kinds"] = dict(Counter(p["kind"] for p in man[n]))
            checks[n]["answers"] = dict(Counter(it["answer"] for p in man[n] for it in p["items"]).most_common(15))
    checks["split_isolation"] = "passed: eval sets mutually image-disjoint, val-only, disjoint from all training pools"

    mdir = droot(cfg, "manifests")
    for n, m in man.items():
        save_json(m, os.path.join(mdir, f"{n}.json"), indent=None)
    sd = results_dir("task2", "splits")
    ids = {}
    for n, m in man.items():
        if n.startswith("caption"):
            ids[n] = {"image_keys": sorted({x["image_key"] for x in m}), "caption_ids": [x["caption_id"] for x in m]}
        else:
            ids[n] = {"pair_ids": [p["pair_id"] for p in m],
                      "question_ids": [it.get("question_id") for p in m for it in p["items"] if it.get("question_id")],
                      "image_keys": sorted({it["image_key"] for p in m for it in p["items"]})}
    save_json(ids, os.path.join(sd, "selected_ids.json"), indent=None)
    save_json(checks, os.path.join(sd, "data_checks.json"))
    print(checks)
    needed = {k: file_of[k] for k in train_keys | set().union(*[keys_of(n) for n in eval_sets])}
    save_json(needed, os.path.join(mdir, "needed_images.json"), indent=None)
    download_images(cfg, needed)


def inspect_labels(cfg, n=24):
    """Render random grounding pairs (train pool + dev/test) to PNG grids for manual label checking,
    independently of any model prediction."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image
    mdir = droot(cfg, "manifests")
    rng = np.random.default_rng(cfg["seed"] + 1)
    od = results_dir("task2", "label_inspection")
    files = load_json(os.path.join(mdir, "needed_images.json"))
    for name in ["presence_pool", "counting_pool", "grounding_dev", "grounding_test"]:
        pairs = load_json(os.path.join(mdir, f"{name}.json"))
        pick = [pairs[k] for k in rng.permutation(len(pairs))[: n // 2]]
        fig, axes = plt.subplots(len(pick), 2, figsize=(6, 3 * len(pick)))
        for r, p in enumerate(pick):
            for c, it in enumerate(p["items"]):
                split, iid = it["image_key"].split(":")
                fn = files[it["image_key"]]
                axes[r, c].imshow(Image.open(droot(cfg, "images", split, fn)).convert("RGB"))
                axes[r, c].set_title(f"{p['question']}\nlabel: {it['answer']}", fontsize=7)
                axes[r, c].axis("off")
        fig.tight_layout()
        fig.savefig(os.path.join(od, f"{name}_sample.png"), dpi=90)
        plt.close(fig)
    print("label inspection grids written to", od, "- record any label errors you find in the report")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/task2.yaml")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    cfg = load_config(a.config, a.set)
    if a.inspect:
        inspect_labels(cfg)
    else:
        prepare(cfg)
