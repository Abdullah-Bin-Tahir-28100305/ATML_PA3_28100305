| method        |   acc |   coverage |   mean_token_cost |
|:--------------|------:|-----------:|------------------:|
| FirstAns      | 53.60 |      82.69 |            277.80 |
| LogP          | 51.91 |      82.69 |           2229.90 |
| SC            | 59.86 |      82.69 |           2229.90 |
| SelfCert      | 54.76 |      82.69 |           2229.90 |
| SelfCert+RWC  | 60.02 |      82.69 |           2229.90 |
| Shortest      | 54.56 |      82.69 |           2229.90 |
| TokFusion     | 55.73 |      82.69 |           2229.90 |
| TokFusion+RWC | 60.89 |      82.69 |           2229.90 |
| VisConf       | 56.26 |      82.69 |           2229.90 |
| VisConf+RWC   | 60.75 |      82.69 |           2229.90 |
| Vision        | 54.33 |      82.69 |           2229.90 |
| Vision+RWC    | 60.05 |      82.69 |           2229.90 |

Paired bootstrap (1,000 question resamples):

|   N | comparison                  |   diff_pp |   ci95_lo |   ci95_hi |
|----:|:----------------------------|----------:|----------:|----------:|
|   8 | SC − FirstAns               |      6.26 |      5.11 |      7.47 |
|   8 | SelfCert+RWC − SC           |      0.16 |     -0.65 |      0.95 |
|   8 | SelfCert+RWC − SelfCert     |      5.26 |      4.04 |      6.54 |
|   8 | SelfCert − LogP             |      2.85 |      1.46 |      4.20 |
|   8 | SelfCert − SC               |     -5.10 |     -6.65 |     -3.55 |
|   8 | LogP − SC                   |     -7.95 |     -9.51 |     -6.35 |
|   8 | Shortest − SC               |     -5.30 |     -6.85 |     -3.80 |
|   8 | Vision+RWC − Vision         |      5.72 |      4.47 |      6.95 |
|   8 | VisConf+RWC − VisConf       |      4.49 |      3.31 |      5.66 |
|   8 | VisConf+RWC − SelfCert+RWC  |      0.72 |     -0.05 |      1.52 |
|   8 | VisConf+RWC − SC            |      0.89 |      0.14 |      1.70 |
|   8 | VisConf+RWC − Vision+RWC    |      0.70 |      0.00 |      1.39 |
|   8 | TokFusion − VisConf         |     -0.54 |     -1.18 |      0.16 |
|   8 | TokFusion+RWC − VisConf+RWC |      0.14 |     -0.23 |      0.49 |
|   8 | VisConf − SelfCert          |      1.50 |      0.04 |      3.02 |