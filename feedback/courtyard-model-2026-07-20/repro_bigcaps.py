import re,sys,os,shutil,subprocess,math,json
S='/private/tmp/claude-501/-Users-andrew/9a7c9df0-e186-4053-a538-2adfe13bbb1b/scratchpad'
sys.path.insert(0,S); import place_engine as pe
MLX=os.path.expanduser('~/Code/mlx-router'); PY=os.path.join(MLX,'.venv/bin/python')
BIG=['C8','C9','C28','C13','C16','C31','C72','C47','R174','R175']
work=os.path.join(S,'bigtest.kicad_pcb')
shutil.copy('/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb',work)
pos=pe.read_positions(work)
pe.apply_positions({r:(0.0,0.0,pos[r][2]) for r in BIG},pcb=work,backup=False)  # pile at origin
out=os.path.join(S,'out/bigtest'); os.makedirs(out,exist_ok=True)
# generous fence in area1: 130x60 = 7800mm2, big parts total ~2200mm2 => 28% util (roomy)
cmd=[PY,'region.py',work,'--region','20,2,130,60','--components',','.join(BIG),
     '--seed','1','--k','1','--pitch','0.5','--layers','F.Cu,B.Cu','--out',out+'/','--json']
r=subprocess.run(cmd,cwd=MLX,capture_output=True,text=True,timeout=600)
print("returncode",r.returncode)
print("STDOUT tail:",r.stdout[-600:])
if r.returncode!=0: print("STDERR:",r.stderr[-800:]); sys.exit()
cand=os.path.join(out,'cand-1.kicad_pcb')
# measure overlaps among BIG in the result
SHAPE=re.compile(r'\(fp_(line|rect|arc|poly|circle)\s(?:(?!\(fp_)[\s\S]){0,700}?\(layer "[FB]\.CrtYd"\)')
def lc(blk):
    xs,ys=[],[]
    for m in SHAPE.finditer(blk):
        t=m.group(0)
        if m.group(1)=='circle':
            c=re.search(r'\(center\s+([-\d.]+)\s+([-\d.]+)\)',t);e=re.search(r'\(end\s+([-\d.]+)\s+([-\d.]+)\)',t)
            if c and e:
                cx,cy=float(c.group(1)),float(c.group(2));rr=((float(e.group(1))-cx)**2+(float(e.group(2))-cy)**2)**.5
                xs+=[cx-rr,cx+rr];ys+=[cy-rr,cy+rr]
        else:
            for xm,ym in re.findall(r'\((?:start|end|xy|mid)\s+([-\d.]+)\s+([-\d.]+)\)',t):xs.append(float(xm));ys.append(float(ym))
    return (min(xs),min(ys),max(xs),max(ys)) if xs else None
src=open(cand).read(); F={}
for a,b in pe.blocks(src,'footprint'):
    blk=src[a:b];mr=re.search(r'\(property "Reference"\s+"([^"]+)"',blk)
    if not mr or mr.group(1) not in BIG: continue
    mat=re.search(r'\(footprint[\s\S]*?\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\)',blk)
    x,y=float(mat.group(1)),float(mat.group(2));rot=float(mat.group(3) or 0);l=lc(blk)
    x0,y0,x1,y1=l;rr=math.radians(rot);cs,sn=math.cos(rr),math.sin(rr);wx=[];wy=[]
    for px,py in[(x0,y0),(x1,y0),(x1,y1),(x0,y1)]:wx.append(x+px*cs-py*sn);wy.append(y+px*sn+py*cs)
    F[mr.group(1)]=(min(wx),min(wy),max(wx),max(wy))
def ov(a,b):
    ix=min(a[2],b[2])-max(a[0],b[0]);iy=min(a[3],b[3])-max(a[1],b[1]);return ix*iy if ix>0 and iy>0 else 0
refs=list(F);bad=[]
for i in range(len(refs)):
    for j in range(i+1,len(refs)):
        o=ov(F[refs[i]],F[refs[j]])
        if o>0.5: bad.append((o,refs[i],refs[j]))
print(f"\nBIG parts placed: {len(F)}/{len(BIG)}  |  overlapping pairs among them: {len(bad)}")
for o,x,y in sorted(bad,reverse=True): print(f"   {o:6.1f}mm2  {x} x {y}")
