"""Extract dated collateral inputs from the issuer PDF. Requires pypdf."""
from pathlib import Path
from datetime import datetime
from decimal import Decimal
import csv
import re
from pypdf import PdfReader

root=Path(__file__).resolve().parent
reader=PdfReader(root/"raw/prospectus.pdf")
def save(name,rows):
    with (root/"data"/(name+".csv")).open("w",encoding="utf-8-sig",newline="") as f:
        out=csv.DictWriter(f,fieldnames=rows[0].keys())
        out.writeheader()
        out.writerows(rows)

schedule=[]
page=reader.pages[63].extract_text()
assert "Original Scheduled Payments" in page
for m,y,value in re.findall(r"([A-Z][a-z]+) (20\d\d) \$?([\d,]+\.\d\d)",page):
    period=datetime.strptime(m+" "+y,"%B %Y").strftime("%Y-%m")
    schedule.append(dict(collection_month=period,gross_scheduled_usd=value.replace(",",""),prospectus_print_page=59))
schedule.sort(key=lambda r:r["collection_month"])
assert len(schedule)==len(set(r["collection_month"] for r in schedule))==73
save("OriginalSchedule",schedule)
composition=[]
for n,dimension in [(59,"apr_band"),(60,"equipment_and_frequency"),(61,"current_balance"),(62,"geography")]:
    group=dimension
    asset=""
    for line in reader.pages[n].extract_text().splitlines():
        line=line.strip()
        if line=="Agricultural" or line=="Construction":
            asset=line
            group="equipment_type"
        if line.startswith("Distribution of the Receivables by Payment Frequency"):
            group="payment_frequency"
            asset=""
        hit=re.fullmatch(r"(.+?)\s+([\d,]+)\s+\$?([\d,]+\.\d\d)\s+([\d.]+)%?(?:\(\d\))?",line)
        if not hit or hit[1]=="Total":continue
        label,count,bal,weight=hit.groups()
        composition.append(dict(dimension=group,bucket=(asset+" "+label).strip(),receivables=int(count.replace(",","")),balance_including_accrued_usd=bal.replace(",",""),reported_balance_share=Decimal(weight)/100,prospectus_print_page=n-4))
for g in set(r["dimension"] for r in composition):
    rows=[r for r in composition if r["dimension"]==g]
    assert sum(r["receivables"] for r in rows)==19156,g
    assert sum(Decimal(r["balance_including_accrued_usd"]) for r in rows)==Decimal("1694001151.31"),g
save("PoolComposition",composition)
