
### grounding (final test, 64 pairs / 128 questions)

| condition | acc (95% CI) | counting | presence | pair acc (95% CI) | identical | empty | = original | = partner | unsupported | mean ref NLL | s/answer |
|---|---|---|---|---|---|---|---|---|---|---|---|
| correct | 71.1 [63.3, 78.9] | 50.0 | 92.2 | 51.6 [39.1, 64.1] | 37.5 | 0.0 | 71.1 | 20.3 | 8.6 | 0.644 | 0.0567 |
| swap | 20.3 [14.1, 27.3] | 32.8 | 7.8 | 3.1 [0.0, 7.8] | 37.5 | 0.0 | 20.3 | 71.1 | 8.6 | 2.535 | 0.0328 |
| mismatched | 35.9 [29.7, 42.2] | 37.5 | 34.4 | 3.1 [0.0, 7.8] | 65.6 | 0.0 | 35.9 | 57.0 | 7.0 | 1.935 | 0.0307 |
| no_image | 48.4 [46.1, 50.0] | 46.9 | 50.0 | 0.0 [0.0, 0.0] | 100.0 | 0.0 | 48.4 | 48.4 | 3.1 | 1.187 | 0.0236 |

Correct-image advantages (pair bootstrap, 4,000 resamples, matched pair IDs):

- vs swap: Δacc = 50.8 [36.7, 64.1] pp; ΔNLL = 1.891 [1.455, 2.356]
- vs mismatched: Δacc = 35.2 [24.2, 46.1] pp; ΔNLL = 1.291 [0.999, 1.610]
- vs no_image: Δacc = 22.7 [14.8, 29.7] pp; ΔNLL = 0.543 [0.423, 0.658]
- counting: Δacc(correct − swap) = 17.2 [0.0, 34.4] pp
- presence: Δacc(correct − swap) = 84.4 [71.9, 96.9] pp

### vqa (final test, 96 pairs / 192 questions)

| condition | acc (95% CI) | number | other | yes/no | pair acc (95% CI) | identical | empty | = original | = partner | unsupported | mean ref NLL | s/answer |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| correct | 49.2 [41.7, 56.3] | 27.5 | 43.5 | 69.4 | 22.9 [14.6, 31.2] | 46.9 | 0.0 | 45.3 | 24.5 | 30.2 | 1.600 | 0.0373 |
| swap | 29.5 [23.3, 35.4] | 17.1 | 17.1 | 50.1 | 3.1 [0.0, 7.3] | 46.9 | 0.0 | 24.5 | 45.3 | 30.2 | 2.558 | 0.0381 |
| mismatched | 35.4 [29.1, 41.7] | 24.2 | 16.7 | 61.5 | 6.2 [2.1, 11.5] | 67.7 | 0.0 | 31.8 | 27.1 | 41.1 | 2.275 | 0.0364 |
| no_image | 27.7 [22.1, 33.1] | 20.6 | 16.7 | 43.3 | 0.0 [0.0, 0.0] | 100.0 | 0.0 | 25.0 | 25.0 | 50.0 | 2.560 | 0.0197 |

Correct-image advantages (pair bootstrap, 4,000 resamples, matched pair IDs):

- vs swap: Δacc = 19.7 [10.9, 28.7] pp; ΔNLL = 0.958 [0.619, 1.332]
- vs mismatched: Δacc = 13.9 [6.6, 21.6] pp; ΔNLL = 0.675 [0.387, 0.987]
- vs no_image: Δacc = 21.6 [14.4, 28.8] pp; ΔNLL = 0.960 [0.682, 1.254]
- number: Δacc(correct − swap) = 10.4 [-5.4, 28.1] pp
- other: Δacc(correct − swap) = 26.4 [12.5, 40.3] pp
- yes/no: Δacc(correct − swap) = 19.3 [3.6, 35.4] pp