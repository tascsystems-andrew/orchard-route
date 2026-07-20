#!/usr/bin/env python3
"""Cohesive floorplan: one tight block per functional group, laid out along the
signal-flow zones. Colors by function family. Renders blocks for review; writes
floorplan.json {group:[x,y,w,h]} for the per-group fill step."""
import re,sys,json,math
S='/private/tmp/claude-501/-Users-andrew/9a7c9df0-e186-4053-a538-2adfe13bbb1b/scratchpad'
sys.path.insert(0,S); import place_engine as pe
AREAS={0:(0.0,-46.99,300.0,33.0),1:(0.25,-0.25,300.0,72.0),2:(0.0,96.0,300.0,69.0)}
LOCKED={'S1','S2','S3','S4','S5','S6','SW2','SW3','DS1'}
src=open('/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb').read()
SHAPE=re.compile(r'\(fp_(line|rect|arc|poly|circle)\s(?:(?!\(fp_)[\s\S]){0,700}?\(layer "[FB]\.CrtYd"\)')
def carea(blk):
    xs,ys=[],[]
    for m in SHAPE.finditer(blk):
        t=m.group(0)
        if m.group(1)=='circle':
            c=re.search(r'\(center\s+([-\d.]+)\s+([-\d.]+)\)',t);e=re.search(r'\(end\s+([-\d.]+)\s+([-\d.]+)\)',t)
            if c and e:
                cx,cy=float(c.group(1)),float(c.group(2));rr=((float(e.group(1))-cx)**2+(float(e.group(2))-cy)**2)**.5;xs+=[cx-rr,cx+rr];ys+=[cy-rr,cy+rr]
        else:
            for xm,ym in re.findall(r'\((?:start|end|xy|mid)\s+([-\d.]+)\s+([-\d.]+)\)',t):xs.append(float(xm));ys.append(float(ym))
    return (max(xs)-min(xs))*(max(ys)-min(ys)) if xs else 3.0
CA={}
for a,b in pe.blocks(src,'footprint'):
    blk=src[a:b];mr=re.search(r'\(property "Reference"\s+"([^"]+)"',blk)
    if mr: CA[mr.group(1)]=carea(blk)
part=json.load(open(S+'/partition-enriched.json'))
G={g['name']:[r for r in g['refs'] if r not in LOCKED] for g in part['groups']}
def gca(name): return sum(CA.get(r,3) for r in G.get(name,[]))
def fam(n):
    if n.startswith('triode'): return 'triode'
    if n.startswith(('pentode','screen','plate_load','nfb_network','cathode_','digital_control','ground_rail')): return 'pentode'
    if n.startswith('eq_band') or n=='eq_shelf_band_7': return 'eqband'
    if n.startswith('eq_'): return 'eq'
    if n.startswith('fx_'): return 'fx'
    if n.startswith('PA_'): return 'pa'
    if n.startswith(('guitar','channel_route')): return 'input'
    if n.startswith(('enc_','voicing')): return 'enc'
    if n.startswith(('oled','midi','mcu','rail','gpio')): return 'ctrl'
    return 'misc'
FAMCOL={'input':'#2ec4b6','triode':'#3fa34d','pentode':'#3a7bd5','eq':'#9b5de5','fx':'#d65db1',
        'pa':'#e8703a','eqband':'#7b6cf6','enc':'#e0b83a','ctrl':'#8a9aa8','misc':'#b0b0b0'}
def gcol(n): return FAMCOL[fam(n)]
def _pack_rows(groups,x0,y0,x1,y1,dens,rows,gap=1.2):
    W=x1-x0; H=y1-y0; rh=(H-(rows+1)*gap)/rows
    place={}; cx=x0+gap; ry=y0+gap; used=1
    for g in groups:
        ta=max(gca(g)/dens,12.0); w=min(W-2*gap, max(6.0, ta/rh))
        if cx+w>x1-gap+0.01:
            cx=x0+gap; ry+=rh+gap; used+=1
        place[g]=[round(cx,2),round(ry,2),round(w,2),round(rh,2)]
        cx+=w+gap
    return place, used
def pack(groups,x0,y0,x1,y1,dens=0.42,gap=1.2):
    for rows in range(2,8):                      # fewest rows that don't overflow
        p,used=_pack_rows(groups,x0,y0,x1,y1,dens,rows,gap)
        if used<=rows: return p, y0+gap+used*((y1-y0-(rows+1)*gap)/rows)
    return _pack_rows(groups,x0,y0,x1,y1,dens,7,gap)[0], y1
FP={}
# area1 signal-flow direction (Voxy back board runs R->L: input at right, PA out at left)
AREA1_DIR='RL'   # 'LR' or 'RL'
# zones in SIGNAL ORDER (input-stage first) with their widths; x-bands assigned per direction
ZONE_ORDER=[
 ('A',94.0,['guitar_input_jack','channel_route_P1','channel_route_T1','channel_route_T2','triode_gain_core_T1','triode_gain_core_T2','triode_plate_nfb_T1','triode_plate_nfb_T2']),
 ('B',61.0,['triode_cathode_bias_T1','triode_cathode_bias_T2','triode_cathode_bypass_T1','triode_cathode_bypass_T2','triode_digital_control']),
 ('C',64.0,['pentode_core','screen_g2','plate_load_output','nfb_network','cathode_bias_sense','cathode_bypass_relays','digital_control','ground_rail_distribution']),
 ('D',45.0,['eq_input_buffer','eq_output_buffer','eq_master_tone_control','eq_core_bus_driver','eq_led_driver','fx_input_send_buffer','fx_output_return_buffer','fx_send_return_opto_jack']),
 ('E',35.75,['PA_phase_inverter','PA_supply_vmid','PA_output_half_1','PA_output_half_2','PA_output_transformer','PA_bias_generator','PA_gate_bias_clamp']),
]
seq=ZONE_ORDER if AREA1_DIR=='LR' else list(reversed(ZONE_ORDER))  # RL: input stage on the right
ZONES=[]; cx=0.25
for z,w,gs in seq:
    ZONES.append((z,cx,cx+w,gs)); cx+=w
a1y0,a1y1=-0.25,71.75
for z,x0,x1,gs in ZONES:
    zca=sum(gca(g) for g in gs); zarea=(x1-x0)*(a1y1-a1y0)
    dz=min(0.58,(zca/zarea)/0.82)                     # tile ~82% of the zone
    p,bot=pack(gs,x0,a1y0,x1,a1y1,dens=dz); FP.update(p)
    print(f"zone {z}: {len(gs)}g dens_eff={zca/zarea:.0%} bot={bot:.1f} (max {a1y1:.1f}){' OVERFLOW' if bot>a1y1 else ''}")
pos=pe.read_positions('/Users/andrew/Documents/Guitar/Voxy/placement-review/voxy_wa.kicad_pcb')
# area0: support blocks tucked just below their encoder (inside area0 y -46.99..-13.99)
for g,sw in [('enc_boom','S1'),('enc_body','S2'),('enc_bite','S3'),('enc_howl','S4'),('enc_sizzle','S5')]:
    if sw in pos:
        w,h=13.0,6.2; FP[g]=[round(pos[sw][0]-w/2,2),-21.0,w,h]
for g,sw in [('voicing_sw2','SW2'),('voicing_sw3','SW3')]:
    if sw in pos: FP[g]=[round(pos[sw][0]-6,2),-21.0,12.0,6.2]
FP['gpio_exp_17']=[16.0,-46.0,24.0,11.5]
# area2: DS1 (43x38 OLED) at x128.7-171.6 splits it into two columns
DS1RECT=(128.7,110.5,171.6,149.0)
colL,_=pack(['oled_display','eq_band_1','eq_band_2','eq_band_3','eq_band_4'],2.0,98.0,126.0,165.0)
colR,_=pack(['midi_io','mcu_control_core','enc_nav','gpio_exp_08','rail_monitor','eq_band_5','eq_band_6','eq_shelf_band_7'],174.0,98.0,298.0,165.0)
FP.update(colL); FP.update(colR)
json.dump(FP,open(S+'/floorplan.json','w'),indent=0)
print(f"floorplan groups placed: {len(FP)}/{len(G)}")
missing=[g for g in G if g not in FP]
if missing: print("MISSING:",missing)
minx,miny,maxx,maxy=-6,-52,306,170; Wd=maxx-minx; Hd=maxy-miny; sc=3.6
o=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{Wd*sc:.0f}" height="{Hd*sc:.0f}" viewBox="{minx} {miny} {Wd} {Hd}" font-family="monospace">']
o.append(f'<rect x="{minx}" y="{miny}" width="{Wd}" height="{Hd}" fill="#0d0d12"/>')
for a,(ax,ay,aw,ah) in AREAS.items():
    o.append(f'<rect x="{ax}" y="{ay}" width="{aw}" height="{ah}" fill="none" stroke="#2b6" stroke-width="0.5"/>')
    o.append(f'<text x="{ax+1}" y="{ay-1.5}" fill="#2b6" font-size="3.5">area{a}</text>')
# DS1 real courtyard keep-out
o.append(f'<rect x="{DS1RECT[0]}" y="{DS1RECT[1]}" width="{DS1RECT[2]-DS1RECT[0]}" height="{DS1RECT[3]-DS1RECT[1]}" fill="#334" stroke="#89b" stroke-width="0.4" stroke-dasharray="1.5"/>')
o.append(f'<text x="{DS1RECT[0]+2}" y="{(DS1RECT[1]+DS1RECT[3])/2}" fill="#9ac" font-size="3">DS1 (OLED)</text>')
for r in LOCKED:
    if r in pos and r!='DS1':
        o.append(f'<circle cx="{pos[r][0]}" cy="{pos[r][1]}" r="2.2" fill="#456" stroke="#89b" stroke-width="0.3"/>')
        o.append(f'<text x="{pos[r][0]+2.8}" y="{pos[r][1]+0.8}" fill="#89b" font-size="2.4">{r}</text>')
for g,(x,y,w,h) in FP.items():
    c=gcol(g)
    o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{c}" fill-opacity="0.55" stroke="{c}" stroke-width="0.4"/>')
    o.append(f'<text x="{x+0.6}" y="{y+3.2}" fill="#fff" font-size="2.1">{g.replace("_"," ")[:18]}</text>')
    o.append(f'<text x="{x+0.6}" y="{y+h-1}" fill="#fff" font-size="1.7" fill-opacity="0.7">{len(G[g])}p</text>')
o.append('</svg>'); open('/Users/andrew/Documents/Guitar/Voxy/placement-review/voxy_floorplan.svg','w').write('\n'.join(o))
print("wrote voxy_floorplan.svg + floorplan.json")
