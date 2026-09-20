import socket, subprocess, time, statistics as st

GW, NAS = "192.168.1.1", "192.168.1.2"

def icmp(host):
    try:
        o = subprocess.run(["ping","-c","1","-W","800","-t","2",host],
                           capture_output=True,text=True,timeout=3)
    except Exception: return None
    for l in o.stdout.splitlines():
        if "time=" in l:
            return float(l.split("time=")[1].split()[0])
    return None

def tcp(host, port=80):
    t=time.perf_counter()
    try:
        s=socket.create_connection((host,port),timeout=2); s.close()
        return (time.perf_counter()-t)*1000
    except Exception:
        return None

s = {"icmp_gw":[], "tcp_gw":[], "icmp_nas":[], "tcp_nas":[]}
for i in range(45):
    for k,f in (("icmp_gw",lambda:icmp(GW)),("tcp_gw",lambda:tcp(GW)),
                ("icmp_nas",lambda:icmp(NAS)),("tcp_nas",lambda:tcp(NAS,5000))):
        v=f()
        if v is not None: s[k].append(v)
    time.sleep(0.4)

for k,v in s.items():
    if not v: print(f"{k}: no data"); continue
    v2=sorted(v)
    p=lambda p: v2[min(len(v2)-1,int(p/100*len(v2)))]
    print(f"{k:>9}: n={len(v2):3d} avg={st.mean(v2):7.2f} p50={p(50):7.2f} "
          f"p90={p(90):7.2f} max={v2[-1]:8.2f}  >20ms:{sum(1 for x in v2 if x>20)}")
