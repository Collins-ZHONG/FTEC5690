"""Rebuild all model tables offline from the two disclosed exhibits per payment date."""
import csv
import json
import re
from pathlib import Path
from datetime import datetime
from decimal import Decimal as D

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data"
OUT.mkdir(exist_ok=True)
dictionary = {}
checks = []
missing = []


def write(name, rows):
    with (OUT / (name + ".csv")).open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def parse(text):
    if re.fullmatch(r"\d{1,2}-[A-Za-z]{3}-\d{2}", text):
        return datetime.strptime(text, "%d-%b-%y").date().isoformat()
    if text in ("Y","N"):
        return text
    num = text.replace(",", "").replace("$","").replace(" ","").replace("¹","")
    if num.startswith("(") and num.endswith(")"):
        num = "-" + num[1:-1]
    if num.endswith("%"):
        return D(num[:-1])/100
    return D(num)


def read_exhibit(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    assert "John Deere Owner Trust 2024-B" in "\n".join(lines[:8])
    section = ""
    entries = []
    for line in lines[1:]:
        cells = [c.strip() for c in line.split("|")]
        if re.fullmatch(r"\(\d+\)", cells[0]):
            section = cells[0].strip("()")
        if len(cells)>1 and cells[-1]:
            try:
                value = parse(cells[-1])
            except ArithmeticError:
                continue
            entries.append((section,cells[-2].rstrip(":").lower(),value))
    return entries


def field(table,name,unit,exhibit,section,prefix,entries,index=0):
    label = re.sub(r"A-(?:2A|2B|1|3|4)", "{tranche}", prefix) if table=="NoteCashflows" else prefix
    if table=="Delinquency":
        label=re.sub(r"30-59|60-89|90-119|120-149|150-179|180 or more|30 or more","{days_past_due}",label)
    dictionary[table,name] = dict(table=table,field=name,unit=unit,source=exhibit,section=section,label=label)
    values=[v for sec,label,v in entries if (not section or sec==section) and label.startswith(prefix.lower())]
    if len(values)<=index:
        missing.append((month,table,name))
        return None
    return values[index]


def check(month,metric,left,right,tol=D(".021")):
    if left is None or right is None:
        checks.append(dict(period=month,check=metric,difference=None,tolerance=tol,status="missing"))
    else:
        diff=left-right
        checks.append(dict(period=month,check=metric,difference=diff,tolerance=tol,status="pass" if abs(diff)<=tol else "review"))


pool_specs = [
("payment_date","date","","Actual Payment Date"),("collection_start","date","","Collection Period Begin"),("collection_end","date","","Collection Period End"),("days_30_360","days","","Days in accrual period (30/360)"),("days_act_360","days","","Days in accrual period (ACT/360)"),("sofr","decimal/year","","One-Month SOFR"),("a2b_rate","decimal/year","","Class A-2B interest rate"),
("pool_balance","USD","12","Pool Balance (excluding"),("note_value","USD","12","Note Value as of"),("overcollateralization","USD","12","Amount of Overcollateralization"),("account_count","count","17","Number of loans at the end"),("account_count_start","count","17","Number of loans at the beginning"),("remaining_term","months","17","Weighted average remaining term"),("weighted_apr","decimal/year","17","Weighted average APR"),("prepayment","USD","17","Prepayment amount - monthly"),("prepayment_cumulative","USD","17","Prepayment amount - life"),("net_loss","USD","15","Aggregate amount of net losses"),("net_loss_cumulative","USD","15","Cumulative amount of net losses"),("net_loss_cumulative_ratio","decimal","15","Cumulative net loss as a percent"),("delinquency_60plus","decimal","16","Payoff Amount of Receivables"),("delinquency_trigger","decimal","18","Delinquency Trigger"),("trigger_occurred","Y/N","18","Delinquency Trigger Occurred"),
("total_distribution","USD","1","Total Distribution Amount"),("cash_receipts","USD","1","Total cash receipts"),("repurchases","USD","1","Administrative repurchases"),("investment_income","USD","1","Investment earnings"),("buyback","USD","1","Payment from Deere"),
("administration_fee","USD","2","Administration Fee"),("review_fee_priority2","USD","10","Section 5.04 (iii)"),("note_interest","USD","3","Noteholders' Interest Distributable Amount deposited"),("note_principal","USD","4","Noteholders' Principal Distribution Amount deposited"),("principal_shortfall","USD","4","Noteholders' Principal Carryover Shortfall"),("interest_shortfall","USD","3","Noteholders' Interest Carryover Shortfall"),("reserve_start","USD","6","Beginning balance"),("reserve_deposit","USD","6","Amount to increase"),("reserve_to_notes","USD","6","Amount to be withdrawn from the Reserve Account and deposited into Note"),("reserve_to_certificate","USD","6","Amount to be withdrawn from the Reserve Account and Deposited into Certificate"),("reserve_end","USD","6","Ending Balance"),("reserve_target","USD","6","Specified Reserve"),("servicing_fee_paid","USD","7","Amount of Servicing Fee paid"),("indenture_trustee_fee","USD","8","Amount paid to Indenture Trustee"),("owner_trustee_fee","USD","9","Amount paid to Owner Trustee"),("review_fee_priority9","USD","10","Section 5.04 (ix)"),("certificate_payment","USD","11","Amount Paid to Certificateholder")]
classes=["A-1","A-2A","A-2B","A-3","A-4"]
initial=[388000000,270000000,251850000,521850000,91711000]
rates=["0.05510","0.0542",None,"0.0520","0.0519"]
pool=[]; notes=[]; arrears=[]; sources=[]
catalog=json.loads((ROOT/"filings.json").read_text())
for month,url in sorted(catalog.items()):
    a=read_exhibit(ROOT/f"raw/tables/{month}_99.3.txt")
    n=read_exhibit(ROOT/f"raw/tables/{month}_99.1.txt")
    p={"period":month}
    for name,unit,sec,prefix in pool_specs:
        if name=="a2b_rate":
            p[name]=field("PoolPerformance",name,unit,"99.1",sec,prefix,n)
        elif name=="principal_shortfall":
            tagged=[v for sec_id,label,v in a if sec_id=="4" and re.fullmatch(r"class a-(1|2a|2b|3|4) noteholders. principal carryover shortfall",label)]
            assert len(tagged)==5
            p[name]=sum(tagged)
            dictionary["PoolPerformance",name]=dict(table="PoolPerformance",field=name,unit="USD",source="99.3",section="4",label="Sum of five class principal carryover shortfalls")
        else:
            p[name]=field("PoolPerformance",name,unit,"99.3",sec,prefix,a)
    for name,prefix,idx in [("pool_balance_start","at beginning",0),("note_value_start","at beginning",1),("gross_losses","Realized losses",0),("accrued_loss_change","Change in accrued loss",0),("recoveries_signed","Proceeds to the Trust",0)]:
        sec="4" if "start" in name else "13"
        p[name]=field("PoolPerformance",name,"USD","99.1",sec,prefix,n,idx)
    pool.append(p)
    assert p["payment_date"][:7]==month
    for i,c in enumerate(classes):
        row={"period":month,"tranche":c,"coupon_rate":D(rates[i]) if rates[i] else p["a2b_rate"]}
        for name,sec,prefix in [("opening_balance","1",f"Outstanding Principal Amount of Class {c} Notes"),("principal_paid","2",f"Class {c} Notes"),("interest_paid","3",f"Class {c} Notes"),("closing_balance","5",f"Outstanding Principal Amount of Class {c} Notes")]:
            row[name]=field("NoteCashflows",name,"USD","99.1",sec,prefix,n)
        notes.append(row)
        if all(row[k] is not None for k in ["opening_balance","principal_paid","closing_balance"]):
            check(month,c+" balance roll",row["opening_balance"]-row["principal_paid"],row["closing_balance"])
        ds=p["days_act_360" if c in ("A-1","A-2B") else "days_30_360"]
        if None not in (row["opening_balance"],row["coupon_rate"],ds):
            check(month,c+" interest accrual",row["opening_balance"]*row["coupon_rate"]*ds/360,row["interest_paid"])
        check(month,c+" closing vs certificate",row["closing_balance"],field("CertificateCheck","balance_"+c,"USD","99.3","13",f"Outstanding Principal Balance of Class {c} Notes",a))
    for bucket in ["30-59","60-89","90-119","120-149","150-179","180 or more","30 or more"]:
        row={"period":month,"days_past_due":bucket}
        for name,unit,prefix in [("payoff_amount","USD",("Total " if bucket=="30 or more" else "")+"Payoff Amount "),("accounts","count","Number of Receivables "),("pool_share","decimal","Payoff Amount ")]:
            label=prefix+bucket+" days past due as "+("a percent" if name=="pool_share" else "of the end")
            row[name]=field("Delinquency",name,unit,"99.1","11",label,n)
        arrears.append(row)
    buckets=arrears[-7:]
    total=buckets[-1]
    for name,tol in [("payoff_amount",D(".04")),("accounts",D("0"))]:
        check(month,"30+ buckets "+name,sum(r[name] for r in buckets[:-1]),total[name],tol)
    check(month,"60+ ratio from buckets",sum(r["payoff_amount"] for r in buckets[1:-1])/p["pool_balance"],p["delinquency_60plus"],D(".000051"))
    group=notes[-5:]
    for k,total in [("interest_paid","note_interest"),("principal_paid","note_principal")]:
        vals=[r[k] for r in group]
        if None not in vals:check(month,k+" sum",sum(vals),p[total])
    if None not in (p["note_value_start"],p["note_value"]):check(month,"note value decline",p["note_value_start"]-p["note_value"],p["note_principal"])
    if all(r["closing_balance"] is not None for r in group):check(month,"overcollateralization",p["note_value"]-sum(r["closing_balance"] for r in group),p["overcollateralization"],D(".51"))
    def summed(names):
        vals=[p[k] for k in names.split()]
        return None if None in vals else sum(vals)
    check(month,"receipts",summed("cash_receipts repurchases investment_income buyback"),p["total_distribution"])
    check(month,"waterfall uses",summed("administration_fee review_fee_priority2 note_interest note_principal reserve_deposit servicing_fee_paid indenture_trustee_fee owner_trustee_fee review_fee_priority9 certificate_payment"),summed("total_distribution reserve_to_notes reserve_to_certificate"),D(".05"))
    check(month,"net loss components",summed("gross_losses accrued_loss_change recoveries_signed"),p["net_loss"])
    if len(pool)>1:
        prev=pool[-2]
        check(month,"pool continuity",p["pool_balance_start"],prev["pool_balance"])
        check(month,"cumulative loss roll",p["net_loss_cumulative"]-prev["net_loss_cumulative"],p["net_loss"])
        check(month,"prepayment cumulative roll",p["prepayment_cumulative"]-prev["prepayment_cumulative"],p["prepayment"])
    check(month,"A-2B coupon reset",p["a2b_rate"],max(D(0),p["sofr"]+D(".0037")),D(".0000001"))
    check(month,"reserve continuity",p["reserve_start"]+p["reserve_deposit"]-p["reserve_to_notes"]-p["reserve_to_certificate"],p["reserve_end"])
    if len(pool)>1:
        check(month,"contract servicing fee",p["pool_balance_start"]/1200,p["servicing_fee_paid"])
        from datetime import date, timedelta
        assert date.fromisoformat(p["collection_start"]) == date.fromisoformat(pool[-2]["collection_end"])+timedelta(days=1)
        for prior,current in zip(notes[-10:-5],group):
            check(month,current["tranche"]+" continuity",prior["closing_balance"],current["opening_balance"])
    for j,r in enumerate(group):
        if r["principal_paid"]>0:
            ahead=group[:1] if j in (1,2) else group[:j]
            check(month,r["tranche"]+" principal priority",sum(v["closing_balance"] for v in ahead),0)
    check(month,"A2 pro rata per original dollar",group[1]["principal_paid"]/initial[1],group[2]["principal_paid"]/initial[2],D(".000000001"))
    acc=url.split("/")[-2]
    sources.append(dict(period=month,form_10d=json.loads((ROOT/"form10d.json").read_text())[month],accession=acc[:10]+"-"+acc[10:12]+"-"+acc[12:],filing_index=url.rsplit("/",1)[0]+"/"+acc[:10]+"-"+acc[10:12]+"-"+acc[12:]+"-index.htm",servicer=url,noteholders=url.replace("ex99d3","ex99d1")))
assert len(pool)==29 and len(notes)==145 and len(arrears)==203
for table, fields in {"PoolPerformance":{"period":"Payment calendar month YYYY-MM"},"NoteCashflows":{"period":"Payment calendar month YYYY-MM","tranche":"Legal note class","coupon_rate":"Annual rate as a decimal, fixed rates from prospectus or A-2B reset"},"Delinquency":{"period":"Payment calendar month YYYY-MM","days_past_due":"Bucket; 30 or more is the inclusive total"}}.items():
    for name,label in fields.items():
        dictionary[table,name]=dict(table=table,field=name,unit="decimal/year" if name=="coupon_rate" else "key",source="prospectus / monthly exhibits",section="",label=label)
write("PoolPerformance",pool);write("NoteCashflows",notes);write("Delinquency",arrears)
write("Sources",sources);write("Validation",checks);write("FieldDictionary",list(dictionary.values()))
print("Months",len(pool),"tranche-months",len(notes),"missing",len(missing))
print("Missing:",missing[:50])
print("Check exceptions:",[(r["period"],r["check"],str(r["difference"])) for r in checks if r["status"]!="pass"][:100])


if missing or any(r["status"]!="pass" for r in checks):
    raise SystemExit("Unresolved reconciliation or missing field; inspect Validation.csv")


