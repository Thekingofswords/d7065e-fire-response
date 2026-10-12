Fire-spread prediction, horizon 300 s, held-out scenarios

| Predictor | AUC | Threshold | Precision | Recall | Onsets | Flagged ≥ 60 s ahead |
|---|---|---|---|---|---|---|
| model | 0.879 | 0.3 | 0.69 | 0.46 | 1546 | 68 % |
| baseline | 0.668 | 0.5 | 0.64 | 0.26 | 1546 | 38 % |

Most important features: smoke_rate_60 (49 %), nb_max_smoke_open (11 %), nb_max_temperature (7 %), nb_max_gradient_open (6 %), nb_max_rate (6 %), nb_max_smoke (5 %), fire_hops (3 %), smoke (3 %)
