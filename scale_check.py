"""Where does the GPU actually start winning? Scale the lattice up."""
import time, numpy as np, mlx.core as mx
from spike_sssp import build_lattice, gpu_sssp, cpu_dijkstra

print(f"{'lattice':>16} {'nodes':>9} {'rounds':>7} {'gpu ms':>9} {'cpu ms':>9} {'speedup':>8}")
for (W,H,L) in [(64,64,6),(128,128,8),(192,192,8),(256,256,8)]:
    rp,ci,wt,N,E = build_lattice(W,H,L)
    t0=time.time(); d,r = gpu_sssp(rp,ci,wt,N,0); mx.eval(d); tg=(time.time()-t0)*1000
    if N <= 150_000:
        t0=time.time(); cpu_dijkstra(rp,ci,wt,N,0); tc=(time.time()-t0)*1000
        sp=f"{tc/tg:.1f}x"
    else:
        tc=float('nan'); sp="(skipped)"
    print(f"{W}x{H}x{L:>2} {N:>9,} {r:>7} {tg:>9.1f} {tc:>9.1f} {sp:>8}")
