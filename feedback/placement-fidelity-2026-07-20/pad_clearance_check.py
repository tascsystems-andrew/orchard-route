#!/usr/bin/env python3
"""DRC-level check my keep-out model never did: pad-copper clearance between
DIFFERENT nets on shared layers. Courtyard non-overlap is necessary but not
sufficient — big pads (FETs, relays) can touch a neighbour with clear bodies."""
import re,sys,math
S='/private/tmp/claude-501/-Users-andrew/9a7c9df0-e186-4053-a538-2adfe13bbb1b/scratchpad'
sys.path.insert(0,S); import place_engine as pe
PCB=sys.argv[1] if len(sys.argv)>1 else '/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb'
CLR=float(sys.argv[2]) if len(sys.argv)>2 else 0.2   # min copper gap
src=open(PCB).read()
pads=[]  # (ref, net, {layers}, cx,cy, hw,hh, rot)
for a,b in pe.blocks(src,'footprint'):
    blk=src[a:b]; mr=re.search(r'\(property "Reference"\s+"([^"]+)"',blk)
    if not mr: continue
    ref=mr.group(1)
    fat=re.search(r'\(footprint[\s\S]*?\(at ([-\d.]+) ([-\d.]+)(?: ([-\d.]+))?\)',blk)
    fx,fy,fr=float(fat.group(1)),float(fat.group(2)),float(fat.group(3) or 0)
    if fx<-40 or fy<-75: continue  # parked
    frad=math.radians(fr); fc,fs=math.cos(frad),math.sin(frad)
    for pm in re.finditer(r'\(pad "[^"]*"\s+\S+\s+\S+\s+\(at ([-\d.]+) ([-\d.]+)(?: ([-\d.]+))?\)\s*\(size ([-\d.]+) ([-\d.]+)\)([\s\S]{0,260}?)(?=\(pad |\Z)',blk):
        px,py,pr,pw,ph,rest=pm.groups(); px,py,pw,ph=map(float,(px,py,pw,ph)); pr=float(pr or 0)
        lm=re.search(r'\(layers ([^)]+)\)',rest); nm=re.search(r'\(net \d*\s*"?([^")]+)"?\)',rest) or re.search(r'\(net "([^"]+)"\)',rest)
        layers=lm.group(1) if lm else '"*.Cu"'; net=nm.group(1).strip().strip('"') if nm else f'NC-{ref}'
        if '.Cu' not in layers and '*.' not in layers: continue
        # world center (footprint frame) + absolute pad rotation
        wx=fx+px*fc-py*fs; wy=fy+px*fs+py*fc; wr=fr+pr
        wl={'F','B'} if ('*.' in layers) else set()
        if 'F.Cu' in layers: wl.add('F')
        if 'B.Cu' in layers: wl.add('B')
        pads.append((ref,net,wl,wx,wy,pw/2,ph/2,wr))
# world AABB of a rotated pad (slightly inflated by CLR/2 so 'gap<CLR' == overlap of inflated)
def aabb(p,pad_clr):
    ref,net,wl,cx,cy,hw,hh,r=p; a=math.radians(r); ca,sa=abs(math.cos(a)),abs(math.sin(a))
    ex=hw*ca+hh*sa+pad_clr; ey=hw*sa+hh*ca+pad_clr
    return (cx-ex,cy-ey,cx+ex,cy+ey)
from collections import defaultdict
grid=defaultdict(list)
BOX={}
for i,p in enumerate(pads):
    r=aabb(p,CLR/2); BOX[i]=r
    for gx in range(int(r[0]//5),int(r[2]//5)+1):
        for gy in range(int(r[1]//5),int(r[3]//5)+1): grid[(gx,gy)].append(i)
def ov(a,b): return a[0]<b[2] and b[0]<a[2] and a[1]<b[3] and b[1]<a[3]
viol=set(); pairs=[]
seen=set()
for cell in grid.values():
    for ii in range(len(cell)):
        for jj in range(ii+1,len(cell)):
            i,j=cell[ii],cell[jj]; k=(i,j) if i<j else (j,i)
            if k in seen: continue
            seen.add(k)
            pi,pj=pads[i],pads[j]
            if pi[0]==pj[0]: continue          # same footprint
            if pi[1]==pj[1]: continue          # same net — OK to touch
            if not (pi[2]&pj[2]): continue     # different layers
            if ov(BOX[i],BOX[j]):              # inflated AABBs overlap => gap < CLR (conservative)
                viol.add(pi[0]); viol.add(pj[0]); pairs.append((pi[0],pi[1],pj[0],pj[1]))
print(f'pad-clearance violations (<{CLR}mm, different nets): {len(pairs)} pad-pairs, {len(viol)} parts')
from collections import Counter
pc=Counter();
for a,na,b,nb in pairs: pc[a]+=1; pc[b]+=1
print('worst parts (violating pad count):')
for r,n in pc.most_common(15): print(f'  {r}: {n}')
print('sample pairs:')
for a,na,b,nb in pairs[:12]: print(f'  {a}({na}) <-> {b}({nb})')
