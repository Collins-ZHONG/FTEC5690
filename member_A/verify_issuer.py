"""Independent comparison with six publisher workbooks. Requires openpyxl."""
from pathlib import Path
import csv
from decimal import Decimal as D
import openpyxl

root=Path(__file__).resolve().parent
def read(name):
    with (root/"data"/(name+".csv")).open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))
pool={r["period"]:r for r in read("PoolPerformance")}
notes={(r["period"],r["tranche"]):r for r in read("NoteCashflows")}
checks=[]
def compare(sheet,period,label,source_value,field,clean,tolerance,cell):
    diff=D(str(source_value))-D(clean[field])
    checks.append(dict(period=period,sheet=sheet,cell=cell,field=field,label=label,issuer_value=source_value,clean_value=clean[field],difference=diff,tolerance=tolerance,status="pass" if abs(diff)<=D(tolerance) else "review"))
for path in sorted((root/"raw").glob("????-??.xlsx")):
    period=path.stem
    book=openpyxl.load_workbook(path,data_only=True)
    s=book["Ex_99.3"]
    n=book["Ex_99.1"]
    assert n["E11"].value.strftime("%Y-%m")==period
    for row,field,tol in [(18,"sofr",".0000001"),(19,"a2b_rate",".0000001"),(21,"total_distribution",".011"),(48,"note_principal",".011"),(90,"servicing_fee_paid",".011"),(101,"certificate_payment",".011"),(103,"pool_balance",".011"),(105,"note_value",".011"),(109,"account_count","0"),(151,"net_loss",".011"),(152,"net_loss_cumulative",".011"),(155,"delinquency_60plus",".000051"),(160,"remaining_term",".0051"),(161,"prepayment",".011"),(162,"prepayment_cumulative",".011"),(163,"weighted_apr",".000051")]:
        c=s.cell(row,5);label=next(str(s.cell(row,col).value) for col in range(4,0,-1) if s.cell(row,col).value is not None)
        compare(s.title,period,label,c.value,field,pool[period],tol,c.coordinate)
    for idx,tranche in enumerate(["A-1","A-2A","A-2B","A-3","A-4"]):
        for base,field in [(18,"opening_balance"),(35,"principal_paid"),(54,"interest_paid"),(85,"closing_balance")]:
            c=n.cell(base+idx*3,5)
            compare(n.title,period,tranche,c.value,field,notes[period,tranche],".011",c.coordinate)
with (root/"data/IssuerCrosscheck.csv").open("w",encoding="utf-8-sig",newline="") as f:
    w=csv.DictWriter(f,fieldnames=checks[0]);w.writeheader();w.writerows(checks)
bad=[r for r in checks if r["status"]!="pass"]
print("Issuer cross-checks:",len(checks),"exceptions:",bad[:3])
assert not bad
