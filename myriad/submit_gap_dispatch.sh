#!/bin/bash -l
# ──────────────────────────────────────────────────────────────────────────────
# Stage the PyPSA-GB gap-demand dispatch campaign on Myriad, one pathway at a time.
#
#   bash -l myriad/submit_gap_dispatch.sh HT          # Holistic Transition  (tasks 1-82)
#   bash -l myriad/submit_gap_dispatch.sh EE           # Electric Engagement  (83-164)
#   bash -l myriad/submit_gap_dispatch.sh HE           # Hydrogen Evolution   (165-246)
#   bash -l myriad/submit_gap_dispatch.sh FB           # Falling Behind       (247-328)
#   bash -l myriad/submit_gap_dispatch.sh all          # everything (1-328)
#   bash -l myriad/submit_gap_dispatch.sh pilot        # HT 2030/2040 x 3 weather yrs (smoke test)
#
# Recommended flow: run `HT` first, eyeball a couple of *_solved.nc, then submit
# EE / HE / FB. Concurrency is throttled with -tc so we never run more than
# $MAXCONC array tasks at once.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="${PYPSA_REPO:-$HOME/Assume_GB/pypsa-gb}"
MAXCONC="${MAXCONC:-20}"          # max concurrent array tasks
QSUB="$REPO/myriad/run_gap_dispatch.qsub"
NAMES="$REPO/myriad/scenario_names.txt"

cd "$REPO"
mkdir -p logs

[ -f "$QSUB" ]  || { echo "missing $QSUB" >&2; exit 1; }
[ -f "$NAMES" ] || { echo "missing $NAMES" >&2; exit 1; }
[ "$(wc -l < "$NAMES")" -eq 328 ] || echo "WARN: scenario_names.txt is not 328 lines"

submit_range() {  # $1=name  $2=range  $3=extra qsub args (optional)
  local name="$1" range="$2" extra="${3:-}"
  echo ">> submitting ${name}: tasks ${range} (max ${MAXCONC} concurrent)"
  qsub -N "gap_${name}" -t "${range}" -tc "${MAXCONC}" ${extra} "$QSUB"
}

STAGE="${1:-}"
case "$STAGE" in
  HT)    submit_range HT 1-82    ;;
  EE)    submit_range EE 83-164  ;;
  HE)    submit_range HE 165-246 ;;
  FB)    submit_range FB 247-328 ;;
  all)   submit_range all 1-328  ;;
  pilot)
    # Full present-cutout subset: every scenario whose weather year is uploaded
    # (see myriad/pilot_scenario_names.txt — 120 scenarios, gap base-fit 2014).
    PILOT="$REPO/myriad/pilot_scenario_names.txt"
    [ -f "$PILOT" ] || { echo "missing $PILOT" >&2; exit 1; }
    N=$(wc -l < "$PILOT")
    echo ">> pilot: $N scenarios from $PILOT"
    SCENARIO_LIST="$PILOT" submit_range pilot "1-${N}" "-v SCENARIO_LIST=$PILOT"
    ;;
  smoke)
    # Single-scenario wiring check: HT30_gap_w2014 (weather + base-fit both present).
    PILOT="$REPO/myriad/pilot_scenario_names.txt"
    n=$(grep -nxF 'HT30_gap_w2014' "$PILOT" | cut -d: -f1)
    echo ">> smoke: HT30_gap_w2014 (pilot line ${n})"
    submit_range smoke "${n}-${n}" "-v SCENARIO_LIST=$PILOT"
    ;;
  *)
    echo "usage: $0 {smoke|pilot|HT|EE|HE|FB|all}" >&2
    echo "  smoke = HT30_gap_w2014 only;  pilot = 120 present-cutout scenarios" >&2
    exit 2
    ;;
esac

echo "--- queue ---"
qstat || true
