#!/bin/bash -l
# ──────────────────────────────────────────────────────────────────────────────
# Submit the FES two-method campaign as staged Myriad array jobs.
#
#   bash -l myriad/submit_fes2methods.sh pilot   # ~6 scenarios, both methods, 3 years
#   bash -l myriad/submit_fes2methods.sh esp      # tasks   1-492  (espeni method)
#   bash -l myriad/submit_fes2methods.sh cmp      # tasks 493-984  (component method)
#   bash -l myriad/submit_fes2methods.sh all      # tasks   1-984  (everything)
#
# Concurrency: MAXCONC (default 80) caps simultaneous array tasks. The ~12 h
# target assumes ~80 tasks run at once at ~45 min each (984 x 0.75 / 80 ≈ 9 h);
# if the scheduler grants fewer, it simply runs longer. Raise/lower with
#   MAXCONC=100 bash -l myriad/submit_fes2methods.sh all
#
# Run the `pilot` stage first and confirm resources/market/<scenario>_balancing.nc
# for both an espeni and a component scenario before scaling out.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

STAGE="${1:-pilot}"
MAXCONC="${MAXCONC:-80}"
QSUB="myriad/run_fes2methods.qsub"
NAMES="myriad/fes2methods_scenario_names.txt"
PILOT="myriad/fes2methods_pilot_names.txt"

case "$STAGE" in
  pilot) RANGE="1-$(wc -l < "$PILOT")"; LIST="$PILOT" ;;
  esp)   RANGE="1-492";   LIST="$NAMES" ;;
  cmp)   RANGE="493-984"; LIST="$NAMES" ;;
  all)   RANGE="1-984";   LIST="$NAMES" ;;
  *) echo "usage: $0 {pilot|esp|cmp|all}" >&2; exit 2 ;;
esac

echo ">> stage=$STAGE  range=$RANGE  list=$LIST  -tc $MAXCONC"
qsub -t "$RANGE" -tc "$MAXCONC" -v SCENARIO_LIST="$PWD/$LIST" "$QSUB"
echo ">> submitted. monitor: qstat ; ls resources/market/*_balancing.nc | wc -l"
