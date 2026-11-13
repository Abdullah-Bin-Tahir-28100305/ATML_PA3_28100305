# data/

Raw datasets and checkpoints are NOT committed. Put the two supplied items here (or point the configs/notebook
at their Drive location):

* `first_plot_questions.yaml` – the 24 qualitative prompts for Task 1 (already included here)
* `ATML-PA3-Data/` – the supplied rollout exports for Task 3 (path: `configs/task3.yaml: data_root`)

Everything else (MetaMathQA, GSM8K, HellaSwag, BoolQ, ARC-Easy, COCO 2014, VQAv2, MathVista metadata, model
weights) is downloaded automatically by the scripts. Large outputs go to `$PA3_OUT` (a Drive folder in Colab).
