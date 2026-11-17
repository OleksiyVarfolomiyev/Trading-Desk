"""Fading the dump phase: a coin that ran up and is now off its 24h high, shorted, net of costs (run research/pump_study.py fetch first)."""
import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parent)); sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
import pump_study as ps
frame=pd.read_csv(ps.FILE, parse_dates=["time"])
def run(runup, off):
    rows=[]
    for coin,part in frame.groupby("coin"):
        part=part.sort_values("time").reset_index(drop=True); c=part["close"]
        peak=c.rolling(24).max(); low=c.rolling(72).min()
        up=(peak/low-1)>=runup; dropped=c<=peak*(1-off)
        hit=(up&dropped).to_numpy(); last=-999
        for t in np.flatnonzero(hit):
            if t-last<48 or t+1+24>=len(part): continue
            last=t; entry=part["open"].iloc[t+1]
            rows.append({"time":part["time"].iloc[t], **{f"r{h}":-(c.iloc[t+h]/entry-1)-ps.COST for h in (4,12,24)}})
    return pd.DataFrame(rows)
def stat(x,time):
    day=time.dt.normalize(); by=x.groupby(day).mean(); half=time>=time.median()
    t=by.mean()/(by.std(ddof=1)/np.sqrt(len(by))) if len(by)>2 and by.std()>0 else np.nan
    return f"{x.mean():+.2%} (hit {(x>0).mean():.0%}, t {t:+.1f}, halves {x[~half].mean():+.2%}/{x[half].mean():+.2%})"
for runup,off in ((0.15,0.05),(0.25,0.05),(0.25,0.08)):
    d=run(runup,off); print(f"fade the dump: run-up>={runup:.0%}, now {off:.0%} off the 24h high, n={len(d)}")
    for h in (4,12,24): print(f"   +{h}h short {stat(d[f'r{h}'],d['time'])}")
