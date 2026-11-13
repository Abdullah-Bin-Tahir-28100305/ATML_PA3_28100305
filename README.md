# EE-5102 / CS-6304 — PA3: Fine-Tuning, Vision-Language Grounding, Test-Time Compute

Code, configurations, split/example IDs, environment records and machine-readable results for all three tasks.
Every reported number should trace to a file under `results/` (small, committed) or `$PA3_OUT` (large, on Drive).

## Repository layout

```
configs/                 task1.yaml, task2.yaml, task3.yaml  (every hyperparameter in one place)
common/                  utils.py (seeding, I/O, timing, env capture), lora.py (LoRA + OPLoRA, merge/export)
task1_llm_finetune/      data.py → train.py → evaluate.py → diagnostics.py → qualitative.py → analyze.py
task2_vlm/               prepare_data.py → cache_features.py → train.py (uses model.py, data.py, evaluate.py)
                         → evaluate.py (final test) → analyze.py ; vqa_eval.py (normalization + soft accuracy)
task3_ttc/               data.py → offline_eval.py → signals.py → adaptive.py → live_generation.py → analyze.py
                         (answers.py: grouping/grading, scoring.py: L, K, V, VisConf, RWC, selection)
run_all_colab.ipynb      single notebook that runs everything (Colab + Google Drive)
results/                 committed machine-readable results, split IDs, figures, env records
data/                    first_plot_questions.yaml (included); put ATML-PA3-Data/ here or on Drive (not committed)
```

## Setup / reproduction

**Colab (recommended):** upload `atml-pa3.zip` to Google Drive, open `run_all_colab.ipynb`, set the paths in the
first cell, run top to bottom. All scripts resume after a disconnect (Task 1/2 recovery checkpoints, Task 3 live
calls are appended to JSONL). An A100 or L4 runtime is strongly recommended (see compute notes).

**Command line** (from the repo root, `export PA3_OUT=/path/for/large/outputs`):

| Task | Commands |
|---|---|
| env | `python -m common.utils --record_env results/env.json` |
| 1 | `python -m task1_llm_finetune.data` · `python -m task1_llm_finetune.train --variant {full_ft,l2sp,lora4,lora16,oplora16}` · `python -m task1_llm_finetune.evaluate --condition {baseline,full_ft_mid,full_ft,l2sp,lora4,lora16,oplora16}` · `python -m task1_llm_finetune.diagnostics` · `python -m task1_llm_finetune.qualitative` · `python -m task1_llm_finetune.analyze` |
| 2 | `python -m task2_vlm.prepare_data` · `python -m task2_vlm.prepare_data --inspect` · `python -m task2_vlm.cache_features` · `python -m task2_vlm.train` · `python -m task2_vlm.evaluate` · `python -m task2_vlm.analyze`  (optional 49-token run: append `--set num_visual_tokens=49 run_name=pool49` to train/evaluate/analyze) |
| 3 | `python -m task3_ttc.data --set data_root=/path/ATML-PA3-Data` · `python -m task3_ttc.offline_eval` · `python -m task3_ttc.signals` · `python -m task3_ttc.adaptive` · `python -m task3_ttc.live_generation --set live.revision=<README revision>` · `python -m task3_ttc.analyze` |

Any config value can be overridden with `--set key.subkey=value`. Seeds are fixed to 6304 everywhere.

## Where each piece of required evidence comes from

### Task 1
| Required evidence | File(s) |
|---|---|
| Benchmark table (7 conditions × 4 benchmarks, primary + Δpp) | `results/task1/analysis/benchmark_table.{md,csv}` |
| Extraction/truncation vs correctness (What-to-watch) | `results/task1/analysis/gsm8k_breakdown.csv` (EM strict/flex, extraction & truncation rates, EM among extractable+untruncated, transitions vs baseline incl. c→w due to unextractable/truncated) |
| Controlled comparisons (rank, L2-SP, OPLoRA, Full vs LoRA-16, mid→final) | `results/task1/analysis/controlled_comparisons.csv` (paired per-example bootstrap CIs) |
| Combined SFT-loss plot; L2-SP penalty; displacement | `loss_curves.png`, `l2sp_penalty.png` (SFT vs total objective plotted separately), `regularization.csv` |
| Resources | `resources.{md,csv}` (trainable params, train time, peak memory, recovery-ckpt & export sizes, L2-SP reference storage/penalty time, OPLoRA basis storage/SVD time/export, skipped updates) |
| Diagnostics (ρ_k, η, R math/non-math) + OPLoRA constraint verification | `diagnostics.png`, `diagnostics_table.csv`, `results/task1/diagnostics/diagnostics.json` |
| Qualitative (168 responses, fixed 42-response annotation subset) | `results/task1/qualitative/responses_168.jsonl`, `side_by_side_all168.md`, `annotation_sheet_42.csv` (fill the `ANN_*` columns yourself), `auto_flag_summary.json` |
| Per-example outputs, extracted answers, truncation flags | `results/task1/eval/<condition>/*_rows.jsonl` (raw lm-eval samples on Drive) |
| Merged-vs-unmerged logit check | `results/task1/eval/<adapter condition>/merge_check.json` |
| Data selection / decontamination / probe IDs | `results/task1/splits/` |

### Task 2
| Required evidence | File(s) |
|---|---|
| Learning curves with stage boundaries (train loss, dev caption loss, answer NLL, swap penalty) | `results/task2/main196/analysis/learning_curves.png`, `dev_metrics.csv` |
| Final tables (both suites × 4 conditions, per-type, pair diagnostics, NLL, advantages + pair-bootstrap CIs) | `analysis/final_tables.{md,csv}`, `final_eval/final_summary.json`, per-item `final_eval/*_predictions.jsonl` |
| Follow-the-partner vs unsupported answers under swaps | `pred_equals_partner_answer_rate`, `pred_unsupported_rate` columns |
| Qualitative complementary pairs (stated selection rule) | `analysis/qualitative_pairs.{png,md}` |
| Resources (caching separated from training; params by stage; gen time) | `analysis/resources.md`, `results/task2/resources/feature_caching.json`, `training_info.json` |
| Interface checks (shapes, frozen sets, connector grads, split isolation, LN applied once, calibration) | `training_info.json:checks`, `results/task2/splits/data_checks.json`, `connector_calibration.json` |
| Label inspection independent of predictions | `results/task2/label_inspection/*.png` |
| Optional 49-token comparison | `results/task2/compression_comparison.md` |

### Task 3
| Required evidence | File(s) |
|---|---|
| Accuracy & oracle-coverage curves, method comparison, failure decomposition | `results/task3/<llm|vlm>/analysis/accuracy_vs_N_and_cost.png`, `coverage_gap.png`, `method_table_N{8,32}.md`, `budget_table.csv`, `failure_decomposition.csv`, `comparisons_bootstrap.csv` |
| Signal analysis (length/score/correctness, conf–vision, tokenwise vs trajectory fusion) | `results/task3/<model>/signals/` (+ TokFusion rows in the method tables) |
| Live inference (accuracy, tokens, runtime, truncation/extraction, transitions) | `results/task3/live/live_summary.json`, `per_question.csv`, `calls.jsonl` |
| Adaptive inference (dev-selected thresholds, held-out accuracy–cost, stop distributions, vs fixed budgets, incorrect early stops) | `results/task3/<model>/adaptive/` |
| Additional samples: new alternatives vs repeats | `new_sample_utility.csv`, `analysis/diversity.png` |
| Qualitative cases (stated rules) | `results/task3/qualitative_cases.md` |
| Scorer version, alignment checks | `results/task3/checks/` |

## Implementation decisions (where the manual leaves a choice)

Task 1
* Chat formatting: prompt = `apply_chat_template([user], add_generation_prompt=True, enable_thinking=False)` (ends with
  the empty `<think>\n\n</think>\n\n` block, identical to inference); target = response + `<|im_end|>`.
  "Formatted length" = prompt + target tokens.
* GSM8K decontamination: lowercase, non-alphanumerics → space, collapse whitespace, exact match on `query` or
  `original_question`.
* L2-SP gradient λ(θ−θ₀) is added to the unscaled gradient before clipping, once per update (exactly equivalent
  to adding λΩ to the objective). θ₀ kept in FP32 on GPU (`ref_device`).
* LR schedule: HF cosine with 38 warmup steps decaying to 0; the scheduler steps even on skipped (overflow) updates.
* lm-eval: a tokenizer copy whose chat template begins with `{%- set enable_thinking = false %}` guarantees the
  non-thinking template regardless of harness version. Truncation flag = re-tokenized response ≥ cap−2 tokens.
* Only the last recovery checkpoint is kept by default (`keep_all_recovery`); sizes of all four are logged.

Task 2
* Prompts: caption instruction "Describe the image briefly."; short-answer suffix "Answer the question using a single
  word or phrase." (LLaVA convention). The 80-token cap applies to the question text; the suffix is appended after.
* Counting answers are digits; presence questions use an article ("a dog"), mass nouns without.
* Grounding pools are built from the 6,000 caption images + 2,000 extra train2014 images; eval grounding = 12+12
  (dev) and 32+32 (test) presence/counting pairs. All eval sets are mutually image-disjoint.
* A fresh AdamW per stage; connector output scale calibrated once on training data (rule saved).
* Mismatched-image map: successor of each pair in a seeded permutation (same side), saved before generation.

Task 3
* MATH groups via math-verify, greedy clustering of unique strings in a gold-independent order; MathVista via the
  official normalization (non-matching multiple-choice text → most similar option, as in MathVista's code).
* Token cost = `n_generated_text_tokens` (retained length). Alignment checks (token IDs vs `logp`/`kl_u_p`/attention
  lists) and the README totals (capped rollouts, null extractions, retained tokens) are written to `results/task3/checks/`.
* MathVista: multiple-choice gold answers are already letters in the export; metadata comes from `AI4Math/MathVista`
  testmini (`pid` = `sample_id`). Following the official code, an integer/float extraction that cannot be parsed as a
  number (e.g. `0.0115 \, \text{m}`) becomes NO_ANSWER.
* Visual engagement v_t = img / (img + prompt_text + generated) attention mass (the three exported totals).
* MATH answer equivalence uses math-verify 0.9.0; a few comparisons hit math-verify's internal timeout and count as
  "not equal" (they print "Timeout during comparison").
* Live prompt = problem + two newlines + instruction (exactly as in the supplied README); boxed extraction for live
  responses uses the export's rule (last literal `\boxed{`, balanced braces, empty/unclosed -> null).

## Compute notes (approximate)
Task 1 Full FT needs ~11–14 GB (FP32 master + AdamW + activations); LoRA ~5–7 GB. On an A100 each run is roughly
0.5–1 h; GSM8K generation dominates evaluation time. Task 2 training ~1–2 h on an A100. Task 3 offline analysis is
CPU-only (minutes); the live experiment generates up to 8,192 tokens/question at batch size 1 (~1–3 h).

## Attribution
* VQA answer normalization (`task2_vlm/vqa_eval.py`) adapted from the official VQA evaluation code (vqaEval.py).
* HellaSwag preprocessing for the probe set copied from lm-evaluation-harness (`tasks/hellaswag/utils.py`).
* MathVista answer normalization follows the official MathVista evaluation logic (normalize_extracted_answer).
* LoRA initialization follows the PEFT convention (Kaiming-uniform A, zero B). Benchmarks: lm-evaluation-harness.
  Equivalence: math-verify. Models: Qwen3-0.6B, SigLIP-base-patch16-224, Qwen2.5-3B-Instruct.
* Code was written with LLM coding assistance (permitted by the course policy); I have reviewed and understand all
  of it. <!-- edit this line to reflect your own use -->
