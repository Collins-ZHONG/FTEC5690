"""Validate release provenance and the dated joins independently of the build."""
from pathlib import Path
from datetime import date, datetime, timedelta
import csv
import hashlib
from decimal import Decimal


root=Path(__file__).resolve().parent
def rows(name):
    with (root/name).open(encoding="utf-8-sig",newline="") as f:
        return list(csv.DictReader(f))
inventory=rows("SourceFiles.csv")
for item in inventory:
    assert hashlib.sha256((root/item["file"]).read_bytes()).hexdigest()==item["sha256"].lower(),item["file"]
pool=rows("data/PoolPerformance.csv")
forms={r["payment_month"]:r for r in rows("data/Form10D.csv")}
sources={r["period"]:r for r in rows("data/Sources.csv")}
assert len(forms)==len(pool)==len(sources)==29
prev_end=None
for r in pool:
    f=forms[r["period"]]
    parse=lambda x: datetime.strptime(x,"%B %d, %Y").date()
    start,end,signed=[parse(f[k]) for k in ("form_distribution_start","form_distribution_end","signature_date")]
    payment=date.fromisoformat(r["payment_date"])
    if end!=payment:
        assert (r["period"],end,payment)==("2026-02",date(2026,12,17),date(2026,2,17))
        assert f["accession_digits"]=="000110465926018876"
    assert prev_end is None or start==prev_end+timedelta(days=1)
    assert start<=payment<=signed<=date(2026,11,25)
    assert f["accession_digits"]==sources[r["period"]]["accession"].replace("-","")
    prev_end=payment
contracts=rows("data/Notes.csv")
assert sum(Decimal(r["original_balance_usd"]) for r in contracts)==1523411000
for v in ["PoolPerformance","NoteCashflows","Delinquency"]:
    fields=set(rows("data/"+v+".csv")[0])
    documented={r["field"] for r in rows("data/FieldDictionary.csv") if r["table"]==v}
    assert fields==documented,(v,fields^documented)
for v in ["Validation","IssuerCrosscheck"]:
    assert all(r["status"]=="pass" for r in rows("data/"+v+".csv"))
print("Verified",len(inventory),"source hashes and",len(forms),"dated filing joins.")

