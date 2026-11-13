| method        |   acc |   coverage |   mean_token_cost |
|:--------------|------:|-----------:|------------------:|
| FirstAns      | 53.60 |      89.50 |            277.80 |
| LogP          | 48.12 |      89.50 |           8897.80 |
| SC            | 61.68 |      89.50 |           8897.80 |
| SelfCert      | 54.62 |      89.50 |           8897.80 |
| SelfCert+RWC  | 62.50 |      89.50 |           8897.80 |
| Shortest      | 53.85 |      89.50 |           8897.80 |
| TokFusion     | 56.25 |      89.50 |           8897.80 |
| TokFusion+RWC | 62.62 |      89.50 |           8897.80 |
| VisConf       | 57.75 |      89.50 |           8897.80 |
| VisConf+RWC   | 63.12 |      89.50 |           8897.80 |
| Vision        | 53.62 |      89.50 |           8897.80 |
| Vision+RWC    | 62.88 |      89.50 |           8897.80 |

Paired bootstrap (1,000 question resamples):

|   N | comparison                  |   diff_pp |   ci95_lo |   ci95_hi |
|----:|:----------------------------|----------:|----------:|----------:|
|  32 | SC − FirstAns               |      8.07 |      6.24 |      9.88 |
|  32 | SelfCert+RWC − SC           |      0.82 |     -0.46 |      2.20 |
|  32 | SelfCert+RWC − SelfCert     |      7.88 |      4.87 |     10.88 |
|  32 | SelfCert − LogP             |      6.50 |      3.38 |      9.75 |
|  32 | SelfCert − SC               |     -7.05 |    -10.05 |     -3.81 |
|  32 | LogP − SC                   |    -13.55 |    -16.55 |    -10.46 |
|  32 | Shortest − SC               |     -7.83 |    -10.89 |     -4.92 |
|  32 | Vision+RWC − Vision         |      9.25 |      6.25 |     12.12 |
|  32 | VisConf+RWC − VisConf       |      5.38 |      2.62 |      8.12 |
|  32 | VisConf+RWC − SelfCert+RWC  |      0.62 |     -0.75 |      2.00 |
|  32 | VisConf+RWC − SC            |      1.45 |      0.05 |      2.96 |
|  32 | VisConf+RWC − Vision+RWC    |      0.25 |     -1.12 |      1.50 |
|  32 | TokFusion − VisConf         |     -1.50 |     -3.50 |      0.38 |
|  32 | TokFusion+RWC − VisConf+RWC |     -0.50 |     -1.12 |      0.12 |
|  32 | VisConf − SelfCert          |      3.12 |      0.25 |      6.38 |