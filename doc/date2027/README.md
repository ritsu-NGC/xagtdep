# date2027 experiments

## EPFL BLIF fixtures

The BLIF files under `epfl_blif/arithmetic/` and `epfl_blif/random_control/`
are checked in as input fixtures for `run_caterpillar_experiments.py`.

- Source repository: `lsils/benchmarks`
- Source commit: `82d8cc6910419298e713a46644ed59fd3df53038` (`master` at import time)
- Upstream paths mirrored here: `arithmetic/*.blif`, `random_control/*.blif`
- License: the upstream repository ships an MIT `LICENSE`; keep the copied
  BLIF fixtures subject to that upstream license notice

Example invocations:

```bash
python doc/date2027/run_caterpillar_experiments.py \
  --epfl-benchmarks all \
  --caterpillar-bin build/blif_to_tcount

python doc/date2027/run_caterpillar_experiments.py \
  --epfl-benchmarks adder max sqrt \
  --kinds random \
  --num-dags 3 \
  --caterpillar-bin build/blif_to_tcount
```
