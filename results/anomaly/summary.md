Anomaly detection, held-out scenarios (threshold: quantile 0.9998 of normal validation scores)

* False anomalies on normal data: 0.45 per 100 zone-hours (1 in 224 zone-hours); validation 0.00
* AUC, event rows vs normal rows: 0.848

| Event | Events | Anomalous within 60 s | within 300 s | Median delay |
|---|---|---|---|---|
| fire | 94 | 100 % | 100 % | 10 s |
| nuisance | 96 | 100 % | 100 % | 10 s |
| fault | 42 | 19 % | 88 % | 40 s |

Cause classifier, share of rows classified correctly:

| Class | Accuracy |
|---|---|
| normal | 99.5 % |
| fire | 89.9 % |
| nuisance | 92.9 % |
| fault | 79.7 % |

Sprinkler releases (hybrid: rules, or anomalous with P(fire) >= 0.98):

| Detector | Fires released | Median delay after ignition | False releases per 100 zone-h |
|---|---|---|---|
| rules | 92/94 | 215 s | 20.27 |
| hybrid | 92/94 | 20 s | 20.55 |
