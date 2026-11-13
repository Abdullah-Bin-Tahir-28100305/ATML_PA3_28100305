| method       |   acc |   coverage |   mean_token_cost |
|:-------------|------:|-----------:|------------------:|
| FirstAns     | 64.35 |      91.38 |            669.77 |
| LogP         | 64.00 |      91.38 |          21347.00 |
| SC           | 73.79 |      91.38 |          21347.00 |
| SelfCert     | 70.62 |      91.38 |          21347.00 |
| SelfCert+RWC | 75.59 |      91.38 |          21347.00 |
| Shortest     | 62.38 |      91.38 |          21347.00 |

Paired bootstrap (1,000 question resamples):

|   N | comparison              |   diff_pp |   ci95_lo |   ci95_hi |
|----:|:------------------------|----------:|----------:|----------:|
|  32 | SC − FirstAns           |      9.44 |      7.97 |     11.01 |
|  32 | SelfCert+RWC − SC       |      1.80 |      0.72 |      2.96 |
|  32 | SelfCert+RWC − SelfCert |      4.96 |      2.84 |      7.21 |
|  32 | SelfCert − LogP         |      6.62 |      4.25 |      9.12 |
|  32 | SelfCert − SC           |     -3.16 |     -5.51 |     -0.82 |
|  32 | LogP − SC               |     -9.79 |    -12.39 |     -7.40 |
|  32 | Shortest − SC           |    -11.41 |    -14.12 |     -8.99 |