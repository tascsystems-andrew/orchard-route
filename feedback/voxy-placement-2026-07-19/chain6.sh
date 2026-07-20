#!/bin/zsh
set -u
SCRATCH=/private/tmp/claude-501/-Users-andrew/9a7c9df0-e186-4053-a538-2adfe13bbb1b/scratchpad
cd ~/Code/mlx-router
BOARD=$SCRATCH/out/bandEW1_s1/cand-1.kicad_pcb
STAT=$SCRATCH/out/chain6.status
: > $STAT
try_seeds() { # name fence refs k -> WAVE_OK/WAVE_BOARD
  local name=$1 fence=$2 refs=$3 k=$4
  WAVE_OK=0
  [ -z "$refs" ] && { WAVE_OK=1; WAVE_BOARD=$BOARD; echo "WAVE $name empty-skip" >> $STAT; return; }
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
  echo "WAVE $name failed all seeds" >> $STAT
}
try_split() { # name fence refs k  (one halving level, then quarters)
  try_seeds "$1" "$2" "$3" "$4"
  [ $WAVE_OK -eq 1 ] && return
  local rl=$3
  local h1=$(python3 -c "r='$rl'.split(',');print(','.join(r[:len(r)//2]))")
  local h2=$(python3 -c "r='$rl'.split(',');print(','.join(r[len(r)//2:]))")
  for part in 1 2; do
    local refs2=$([ $part -eq 1 ] && echo $h1 || echo $h2)
    try_seeds "$1.h$part" "$2" "$refs2" "$4"
    if [ $WAVE_OK -eq 1 ]; then BOARD=$WAVE_BOARD
    else
      local q1=$(python3 -c "r='$refs2'.split(',');print(','.join(r[:len(r)//2]))")
      local q2=$(python3 -c "r='$refs2'.split(',');print(','.join(r[len(r)//2:]))")
      for q in 1 2; do
        local refs3=$([ $q -eq 1 ] && echo $q1 || echo $q2)
        try_seeds "$1.h$part.q$q" "$2" "$refs3" "$4"
        [ $WAVE_OK -eq 1 ] && BOARD=$WAVE_BOARD
      done
    fi
  done
}
F() { python3 -c "import json;f=json.load(open('$SCRATCH/fences2.json'));print(','.join(str(x) for x in f['$1']))"; }
for b in bandA bandD bandE; do
  try_seeds ${b}W2a "$(F $b)" "$(cat $SCRATCH/${b}2_missedbigs.txt)" 1
  [ $WAVE_OK -eq 1 ] && BOARD=$WAVE_BOARD
  try_split ${b}W2b "$(F $b)" "$(cat $SCRATCH/${b}2_smalls2.txt)" 2
done
echo "CHAIN6_DONE final=$BOARD" >> $STAT
