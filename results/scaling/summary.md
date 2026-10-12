| Zones | Agent step, rules (s) | Agent step, model (s) | Physics tick (s) | Bridge req/s without deadband |
|---|---|---|---|---|
| 956 | 0.0303 | 0.0424 | 0.0469 | 1912 |
| 1912 | 0.1037 | 0.0977 | 0.0308 | 3824 |
| 3824 | 0.1318 | 0.1623 | 0.0414 | 7648 |
| 7648 | 0.2959 | 0.3733 | 0.046 | 15296 |
| 15296 | 0.6105 | 0.7863 | 0.1029 | 30592 |

Budgets: decision interval 2.0 s, simulator tick 2.0 s, bridge 150 requests/s.
* bridge_rps_no_deadband exceeds its budget at 956 zones
