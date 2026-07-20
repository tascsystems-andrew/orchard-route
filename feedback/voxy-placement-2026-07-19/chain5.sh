#!/bin/zsh
set -u
SCRATCH=/private/tmp/claude-501/-Users-andrew/9a7c9df0-e186-4053-a538-2adfe13bbb1b/scratchpad
cd ~/Code/mlx-router
BOARD=$SCRATCH/seeded_area1.kicad_pcb
STAT=$SCRATCH/out/chain5.status
: > $STAT
try_wave() { # name fence refs k -> sets WAVE_OK and WAVE_BOARD
  local name=$1 fence=$2 refs=$3 k=$4
  WAVE_OK=0
  for s in 1 2 3 4 5 6; do
    local out=$SCRATCH/out/${name}_s$s
    mkdir -p $out
    .venv/bin/python region.py "$BOARD" --region "$fence" --components "$refs" \
      --seed $s --k $k --pitch 0.5 --layers F.Cu,B.Cu --out "$out/" --json > "$out/run.log" 2>&1
    if [ $? -eq 0 ] && [ -f "$out/cand-1.kicad_pcb" ]; then
      echo "WAVE $name ok seed=$s" >> $STAT
      WAVE_BOARD="$out/cand-1.kicad_pcb"; WAVE_OK=1; return
    fi
  done
  echo "WAVE $name FAILED all seeds" >> $STAT
}
band() {
  local name=$1 fence=$2
  try_wave ${name}W1 "$fence" "$(cat $SCRATCH/${name}2_bigs.txt)" 1
  if [ $WAVE_OK -eq 1 ]; then
    BOARD=$WAVE_BOARD
    try_wave ${name}W2 "$fence" "$(cat $SCRATCH/${name}2_smalls.txt)" 3
    if [ $WAVE_OK -eq 1 ]; then BOARD=$WAVE_BOARD; fi
  fi
}
F() { python3 -c "import json;f=json.load(open('$SCRATCH/fences2.json'));print(','.join(str(x) for x in f['$1']))"; }
band bandA "$(F bandA)"
band bandB "$(F bandB)"
band bandC "$(F bandC)"
band bandD "$(F bandD)"
band bandE "$(F bandE)"
echo "CHAIN5_DONE final=$BOARD" >> $STAT
