#!/bin/zsh
# Area-1 banded placement chain: each band runs on the previous winner.
set -u
SCRATCH=/private/tmp/claude-501/-Users-andrew/9a7c9df0-e186-4053-a538-2adfe13bbb1b/scratchpad
SRC=/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb
cd ~/Code/mlx-router
BOARD=$SRC
run_band() {
  local name=$1 fence=$2 refs=$3; shift 3
  local out=$SCRATCH/out/$name
  mkdir -p $out
  .venv/bin/python region.py "$BOARD" --region "$fence" --components "$refs" "$@" \
      --k 3 --pitch 0.5 --layers F.Cu,B.Cu --out "$out/" --json > "$out/run.log" 2>&1
  local ec=$?
  echo "BAND $name exit=$ec" >> $SCRATCH/out/chain.status
  if [ $ec -eq 0 ] && [ -f "$out/cand-1.kicad_pcb" ]; then
    BOARD="$out/cand-1.kicad_pcb"
    echo "BAND $name -> $BOARD" >> $SCRATCH/out/chain.status
  else
    echo "BAND $name FAILED, chain continues on previous board" >> $SCRATCH/out/chain.status
  fi
}
: > $SCRATCH/out/chain.status
run_band bandA "0.25,-0.25,70,72"   "$(cat $SCRATCH/bandA_refs.txt)"
run_band bandB "70.25,-0.25,60,72"  "$(cat $SCRATCH/bandB_refs.txt)"
run_band bandC "130.25,-0.25,85,72" "$(cat $SCRATCH/bandC_refs.txt)"
run_band bandD "215.25,-0.25,30,72" "$(cat $SCRATCH/bandD_refs.txt)"
run_band bandE "245.25,-0.25,55,72" "$(cat $SCRATCH/bandE_refs.txt)"
echo "CHAIN_DONE final=$BOARD" >> $SCRATCH/out/chain.status
