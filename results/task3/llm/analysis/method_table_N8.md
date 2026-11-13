| method       |   acc |   coverage |   mean_token_cost |
|:-------------|------:|-----------:|------------------:|
| FirstAns     | 64.35 |      84.35 |            669.77 |
| LogP         | 67.54 |      84.35 |           5344.10 |
| SC           | 71.79 |      84.35 |           5344.10 |
| SelfCert     | 70.14 |      84.35 |           5344.10 |
| SelfCert+RWC | 72.89 |      84.35 |           5344.10 |
| Shortest     | 64.56 |      84.35 |           5344.10 |

Paired bootstrap (1,000 question resamples):

|   N | comparison              |   diff_pp |   ci95_lo |   ci95_hi |
|----:|:------------------------|----------:|----------:|----------:|
|   8 | SC − FirstAns           |      7.44 |      6.34 |      8.51 |
|   8 | SelfCert+RWC − SC       |      1.10 |      0.53 |      1.70 |
|   8 | SelfCert+RWC − SelfCert |      2.75 |      1.89 |      3.75 |
|   8 | SelfCert − LogP         |      2.60 |      1.34 |      3.75 |
|   8 | SelfCert − SC           |     -1.65 |     -2.88 |     -0.47 |
|   8 | LogP − SC               |     -4.25 |     -5.56 |     -2.96 |
|   8 | Shortest − SC           |     -7.22 |     -8.75 |     -5.71 |