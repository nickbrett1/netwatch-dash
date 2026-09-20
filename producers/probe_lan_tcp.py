import socket, subprocess, time, statistics as st
NAS="192.168.1.2"; GW="192.168.1.1"; PS5="192.168.1.30"
def icmp(h):
    try: o=subprocess.run(["ping","-c","1","-W","800","-t","2",h],capture_output=True,text=True,timeout=3)
    except Exception: return None
    for l in o.stdout.splitlines():
        if "time=" in l: return float(l.split("time=")[1].split()[0])
    return None
def tcp(h,p=80):
    t=time.perf_counter()
    try:
        s=socket.create_connection((h,p),timeout=2); s.close(); return (time.perf_counter()-t)*1000
    except Exception: return None
s={"icmp_nas":[], "tcp_nas80":[], "icmp_gw":[], "tcp_gw80":[]}
for i in range(30):
    for k,f in (("icmp_nas",lambda:icmp(NAS)),("tcp_nas80",lambda:tcp(NAS,80)),
                ("icmp_gw",lambda:icmp(GW)),("tcp_gw80",lambda:tcp(GW,80))):
        v=f()
        if v is not None: s[k].append(v)
    time.sleep(0.3)
for k,v in s.items():
    if not v: print(f"{k}: NO DATA"); continue
    v2=sorted(v); p=lambda q: v2[min(len(v2)-1,int(q/100*len(v2)))]
    print(f"{k:>11}: n={len(v2):3d} avg={st.mean(v2):7.2f} p50={p(50):6.2f} p90={p(90):7.2f} max={v2[-1]:8.2f} >20ms:{sum(1 for x in v2 if x>20)}")
