#!/bin/zsh
set -u
SCRATCH=/private/tmp/claude-501/-Users-andrew/9a7c9df0-e186-4053-a538-2adfe13bbb1b/scratchpad
cd ~/Code/mlx-router
BOARD=$SCRATCH/seeded_area1.kicad_pcb
run_band() {
  local name=$1 fence=$2 refs=$3
  local out=$SCRATCH/out/${name}_v2
  mkdir -p $out
  .venv/bin/python region.py "$BOARD" --region "$fence" --components "$refs" \
      --k 3 --pitch 0.5 --layers F.Cu,B.Cu --out "$out/" --json > "$out/run.log" 2>&1
  local ec=$?
  echo "BAND $name exit=$ec" >> $SCRATCH/out/chain2.status
  if [ $ec -eq 0 ] && [ -f "$out/cand-1.kicad_pcb" ]; then
    BOARD="$out/cand-1.kicad_pcb"
  else
    echo "BAND $name FAILED" >> $SCRATCH/out/chain2.status
  fi
}
: > $SCRATCH/out/chain2.status
FA=$(python3 -c "import json;f=json.load(open('$SCRATCH/fences2.json'));print(','.join(str(x) for x in f['bandA']))")
FB=$(python3 -c "import json;f=json.load(open('$SCRATCH/fences2.json'));print(','.join(str(x) for x in f['bandB']))")
FC=$(python3 -c "import json;f=json.load(open('$SCRATCH/fences2.json'));print(','.join(str(x) for x in f['bandC']))")
FD=$(python3 -c "import json;f=json.load(open('$SCRATCH/fences2.json'));print(','.join(str(x) for x in f['bandD']))")
FE=$(python3 -c "import json;f=json.load(open('$SCRATCH/fences2.json'));print(','.join(str(x) for x in f['bandE']))")
run_band bandA "$FA" "$(cat $SCRATCH/bandA2_refs.txt)"
run_band bandB "$FB" "$(cat $SCRATCH/bandB2_refs.txt)"
run_band bandC "$FC" "$(cat $SCRATCH/bandC2_refs.txt)"
run_band bandD "$FD" "$(cat $SCRATCH/bandD2_refs.txt)"
run_band bandE "$FE" "$(cat $SCRATCH/bandE2_refs.txt)"
echo "CHAIN2_DONE final=$BOARD" >> $SCRATCH/out/chain2.status
