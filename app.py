import os, json, sqlite3, csv, io, shutil, webbrowser, zipfile, xml.etree.ElementTree as ET
from pathlib import Path
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import date, datetime, timedelta
from html import escape

BASE = os.path.dirname(os.path.abspath(__file__))

# Use Vercel's writable temporary directory
DATA_DIR = os.environ.get(
    'DATA_DIR',
    '/tmp/data' if os.environ.get('VERCEL') else os.path.join(BASE, 'data')
)

os.makedirs(DATA_DIR, exist_ok=True)

DB = os.environ.get(
    'DATABASE_PATH',
    os.path.join(DATA_DIR, 'microloan.db')
)
# Production configuration
CONFIG_PATH=os.path.join(BASE,'config.json')
DEFAULT_CONFIG={'host':'0.0.0.0','port':5050,'auto_open_browser':False,'backup_on_start':True,'backup_retention_days':30}

def load_config():
    cfg=dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH,'r',encoding='utf-8') as f:
                user=json.load(f)
            if isinstance(user,dict): cfg.update(user)
    except Exception:
        pass
    try: cfg['port']=int(cfg.get('port',5050))
    except Exception: cfg['port']=5050
    cfg['host']=str(cfg.get('host','0.0.0.0'))
    return cfg

CONFIG=load_config()
HOST='0.0.0.0'
PORT=int(os.environ.get('PORT',CONFIG['port']))
LOG_DIR=os.path.join(DATA_DIR,'logs')
BACKUP_DIR=os.path.join(DATA_DIR,'backups')

def log(message):
    os.makedirs(LOG_DIR,exist_ok=True)
    stamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(os.path.join(LOG_DIR,'application.log'),'a',encoding='utf-8') as f:
        f.write(f'[{stamp}] {message}\n')

def startup_backup():
    if not CONFIG.get('backup_on_start',True) or not os.path.exists(DB):
        return
    os.makedirs(BACKUP_DIR,exist_ok=True)
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    target=os.path.join(BACKUP_DIR,f'microloan_startup_{stamp}.db')
    try:
        shutil.copy2(DB,target)
        retention=int(CONFIG.get('backup_retention_days',30))
        cutoff=datetime.now()-timedelta(days=retention)
        for p in Path(BACKUP_DIR).glob('microloan_*.db'):
            try:
                if datetime.fromtimestamp(p.stat().st_mtime)<cutoff:
                    p.unlink()
            except Exception:
                pass
        log(f'Startup backup created: {target}')
    except Exception as e:
        log(f'Startup backup failed: {e}')


def conn():
    os.makedirs(os.path.dirname(DB),exist_ok=True)
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def money(v): return round(float(v or 0),2)

def init_db():
    c=conn(); c.executescript('''
    CREATE TABLE IF NOT EXISTS branches(id INTEGER PRIMARY KEY AUTOINCREMENT,code TEXT NOT NULL UNIQUE,name TEXT NOT NULL,address TEXT,manager TEXT,active INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,mobile TEXT,whatsapp TEXT,address TEXT,notes TEXT,branch_id INTEGER,legacy_code TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(branch_id) REFERENCES branches(id));
    CREATE TABLE IF NOT EXISTS loans(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_id INTEGER NOT NULL,branch_id INTEGER,amount REAL NOT NULL,interest_type TEXT NOT NULL,interest_rate REAL DEFAULT 0,total_payable REAL NOT NULL,daily_due REAL NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL,duration_days INTEGER NOT NULL,frequency TEXT DEFAULT 'Daily',status TEXT DEFAULT 'ACTIVE',loan_type TEXT DEFAULT 'ORIGINAL',parent_loan_id INTEGER,opening_balance REAL DEFAULT 0,new_cash_amount REAL DEFAULT 0,cycle_base REAL DEFAULT 0,legacy_ref TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(branch_id) REFERENCES branches(id));
    CREATE TABLE IF NOT EXISTS schedules(id INTEGER PRIMARY KEY AUTOINCREMENT,loan_id INTEGER NOT NULL,due_date TEXT NOT NULL,expected REAL NOT NULL,paid REAL DEFAULT 0,FOREIGN KEY(loan_id) REFERENCES loans(id));
    CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT,loan_id INTEGER NOT NULL,customer_id INTEGER NOT NULL,branch_id INTEGER,payment_date TEXT NOT NULL,amount REAL NOT NULL,mode TEXT DEFAULT 'Cash',reference TEXT,notes TEXT,legacy_ref TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(loan_id) REFERENCES loans(id),FOREIGN KEY(customer_id) REFERENCES customers(id),FOREIGN KEY(branch_id) REFERENCES branches(id));
    CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,branch_id INTEGER,expense_date TEXT NOT NULL,category TEXT NOT NULL,amount REAL NOT NULL,mode TEXT DEFAULT 'Cash',description TEXT,employee TEXT,frequency TEXT DEFAULT 'One-time',legacy_ref TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(branch_id) REFERENCES branches(id));
    CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT NOT NULL,entity TEXT NOT NULL,entity_id INTEGER,details TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    ''')
    def addcol(table,name,typ):
        cols={r['name'] for r in c.execute(f'PRAGMA table_info({table})').fetchall()}
        if name not in cols: c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {typ}')
    addcol('customers','branch_id','INTEGER'); addcol('customers','active','INTEGER DEFAULT 1'); addcol('loans','branch_id','INTEGER');
    c.execute('UPDATE customers SET active=1 WHERE active IS NULL')
    addcol('payments','branch_id','INTEGER'); addcol('expenses','branch_id','INTEGER')
    c.execute("INSERT OR IGNORE INTO branches(code,name) VALUES('MAIN','Main Branch')")
    main=c.execute("SELECT id FROM branches WHERE code='MAIN'").fetchone()['id']
    c.execute('UPDATE customers SET branch_id=? WHERE branch_id IS NULL',(main,))
    c.execute('UPDATE loans SET branch_id=(SELECT branch_id FROM customers WHERE customers.id=loans.customer_id) WHERE branch_id IS NULL')
    c.execute('UPDATE payments SET branch_id=(SELECT branch_id FROM loans WHERE loans.id=payments.loan_id) WHERE branch_id IS NULL')
    c.execute('UPDATE expenses SET branch_id=? WHERE branch_id IS NULL',(main,))
    c.commit(); c.close()

def audit(c,action,entity,eid,details): c.execute('INSERT INTO audit_log(action,entity,entity_id,details) VALUES(?,?,?,?)',(action,entity,eid,details))

def seed():
    # Production edition intentionally starts with an empty database.
    return

def calc_total(amount,itype,rate,supplied):
    amount=money(amount); rate=money(rate)
    if itype=='Flat Interest': return money(amount+rate)
    if itype=='Percentage': return money(amount+amount*rate/100)
    return money(supplied)

def parse_amount(d,k): return float(d.get(k) or 0)
def fmt(v): return f'₹{money(v):,.2f}'

def page(title,body,branch=''):
    c=conn(); brs=c.execute('SELECT id,code,name FROM branches WHERE active=1 ORDER BY name').fetchall(); c.close()
    opts='<option value="">All Branches</option>'+''.join(f'<option value="{r["id"]}" {"selected" if str(r["id"])==str(branch) else ""}>{escape(r["code"])} — {escape(r["name"])}</option>' for r in brs)
    nav=f'''<nav><b>MicroLoan Manager V1.5</b><a href="/">Dashboard</a><a href="/branches">Branches</a><a href="/customers">Customers</a><a href="/loans">Loans</a><a href="/topups">Top-Ups</a><a href="/collections">Collections</a><a href="/outstanding">Outstanding</a><a href="/expenses">Expenses</a><a href="/reports">Reports</a><a href="/audit">Audit</a><a href="/backup">Backup</a><a href="/legacy-import">Legacy Load</a><form action="" method="get" style="display:flex;gap:6px;align-items:center"><b>Branch</b><select name="branch" onchange="this.form.submit()">{opts}</select></form><form action="/search" method="get"><input name="q" placeholder="Search customer / mobile / loan"><button>Search</button></form></nav>'''
    
    css = '''
* {
    box-sizing: border-box;
}

html, body {
    width: 100%;
    max-width: 100%;
    margin: 0;
    overflow-x: hidden;
}

body {
    font-family: Arial, sans-serif;
    background: #f4f6f8;
    color: #18212b;
    font-size: 15px;
}

/* Navigation */
nav {
    background: #172331;
    color: white;
    padding: 14px 22px;
    display: flex;
    gap: 16px;
    align-items: center;
    flex-wrap: wrap;
}

nav a {
    color: white;
    text-decoration: none;
    font-weight: 600;
}

nav form {
    margin-left: auto;
}

/* Main content */
main {
    width: 100%;
    max-width: 1600px;
    padding: 26px;
    margin: auto;
}

.panel {
    background: white;
    border-radius: 12px;
    padding: 22px;
    margin: 16px 0;
    box-shadow: 0 2px 10px #0001;
}

/* Dashboard layouts */
.grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 18px;
}

.cards {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 14px;
}

.card {
    min-width: 0;
    background: white;
    padding: 18px;
    border-radius: 10px;
    box-shadow: 0 2px 10px #0001;
}

.big {
    font-size: 26px;
    font-weight: bold;
    margin-top: 6px;
}

/* Forms */
label {
    display: block;
    margin: 10px 0 5px;
    font-weight: 600;
}

input, select, textarea, button {
    padding: 11px;
    font-size: 16px;
    max-width: 100%;
    border-radius: 5px;
}

input, select, textarea {
    width: 100%;
    min-width: 0;
    border: 1px solid #aaa;
}

textarea {
    min-height: 80px;
}

button {
    background: #1976b8;
    color: white;
    border: 0;
    border-radius: 6px;
    cursor: pointer;
    min-height: 42px;
}

.linkbtn {
    display: inline-block;
    padding: 10px 13px;
    background: #1976b8;
    color: white;
    border-radius: 6px;
    text-decoration: none;
}

.danger { background: #c62828; }
.warn { background: #fff4cc; }
.ok { background: #e3f7e9; }
.bad { background: #fde8e8; }

.calc {
    font-size: 22px;
    font-weight: bold;
    padding: 14px;
    background: #eef6ff;
    border-radius: 8px;
    margin-top: 14px;
}

/* Tables */
.tablewrap {
    width: 100%;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
}

table {
    width: 100%;
    border-collapse: collapse;
}

th, td {
    padding: 10px;
    border-bottom: 1px solid #ddd;
    text-align: left;
    white-space: nowrap;
}

.muted, small { color: #667; }
.right { text-align: right; }

/* Tablet */
@media (max-width: 1000px) {
    main {
        padding: 18px;
    }

    .grid, .cards {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}

/* Mobile */
@media (max-width: 700px) {
    body {
        font-size: 14px;
    }

    main {
        padding: 12px;
    }

    nav {
        padding: 12px;
        gap: 12px;
    }

    nav form {
        margin-left: 0;
        width: 100%;
    }

    nav a {
        font-size: 14px;
    }

    .grid, .cards {
        grid-template-columns: minmax(0, 1fr);
        gap: 10px;
    }

    .panel {
        padding: 14px;
        margin: 10px 0;
    }

    .card {
        padding: 14px;
    }

    .big {
        font-size: 22px;
    }

    h1 { font-size: 23px; }
    h2 { font-size: 20px; }
    h3 { font-size: 17px; }

    button, .linkbtn {
        min-height: 44px;
    }

    .tablewrap table {
        min-width: 600px;
    }
}

/* Small phones */
@media (max-width: 380px) {
    main {
        padding: 8px;
    }

    .panel {
        padding: 11px;
    }

    nav {
        gap: 9px;
    }
}

/* TEMPORARY MOBILE TEST */
@media (max-width: 700px) {
    body {
        background: yellow !important;
    }

    main {
        border: 5px solid red !important;
    }
}
'''

    return f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title><style>{css}</style></head><body>{nav}<main>{body}</main></body></html>'

def send_html(h,html,status=200):
    data=html.encode(); h.send_response(status); h.send_header('Content-Type','text/html; charset=utf-8'); h.send_header('Content-Length',str(len(data))); h.end_headers(); h.wfile.write(data)
def redirect(h,path): h.send_response(303); h.send_header('Location',path); h.end_headers()
def form_data(h):
    n=int(h.headers.get('Content-Length','0')); raw=h.rfile.read(n).decode(); return {k:v[0] for k,v in parse_qs(raw,keep_blank_values=True).items()}

def active_balance(c,lid):
    r=c.execute('SELECT l.total_payable-COALESCE((SELECT SUM(amount) FROM payments p WHERE p.loan_id=l.id),0) bal FROM loans l WHERE l.id=?',(lid,)).fetchone(); return money(r['bal'] if r else 0)

def arrears_info(c,loan,today=None):
    today=date.fromisoformat(today) if isinstance(today,str) else (today or date.today())
    sched=c.execute('SELECT due_date,expected FROM schedules WHERE loan_id=? AND due_date<=? ORDER BY due_date',(loan['id'],today.isoformat())).fetchall()
    pays=c.execute('SELECT payment_date,amount FROM payments WHERE loan_id=? AND payment_date<=? ORDER BY payment_date,id',(loan['id'],today.isoformat())).fetchall()
    cum=0; pi=0; streak=0; maxstreak=0; missed_month=0; month=today.strftime('%Y-%m')
    for s in sched:
        cum+=float(s['expected'])
        while pi<len(pays) and pays[pi]['payment_date']<=s['due_date']:
            # cumulative account-level payment through this due date
            # advance/late behavior is intentionally evaluated at end of day
            pi+=1
        paid=sum(float(p['amount']) for p in pays if p['payment_date']<=s['due_date'])
        behind=cum-paid>0.005
        if behind: streak+=1
        else: streak=0
        maxstreak=max(maxstreak,streak)
        if s['due_date'].startswith(month) and behind: missed_month+=1
    return maxstreak,missed_month


XLSX_NS='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL_NS='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'

def xlsx_colnum(ref):
    letters=''.join(ch for ch in ref if ch.isalpha()).upper(); n=0
    for ch in letters: n=n*26+(ord(ch)-64)
    return n

def excel_date(v):
    if v is None or str(v).strip()=='': return ''
    x=str(v).strip()
    try:
        if x.replace('.','',1).isdigit() and float(x)>20000:
            return (date(1899,12,30)+timedelta(days=float(x))).isoformat()
    except Exception: pass
    return x[:10] if len(x)>=10 and x[4]=='-' and x[7]=='-' else x

def read_xlsx(data):
    z=zipfile.ZipFile(io.BytesIO(data)); shared=[]
    if 'xl/sharedStrings.xml' in z.namelist():
        root=ET.fromstring(z.read('xl/sharedStrings.xml'))
        for si in root.findall(XLSX_NS+'si'): shared.append(''.join(t.text or '' for t in si.iter(XLSX_NS+'t')))
    wb=ET.fromstring(z.read('xl/workbook.xml')); rels=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    relmap={r.attrib['Id']:r.attrib['Target'] for r in rels}; out={}
    for sh in wb.find(XLSX_NS+'sheets'):
        rid=sh.attrib.get(REL_NS+'id'); target=relmap[rid]
        target=target.lstrip('/')
        if not target.startswith('xl/'): target='xl/'+target
        root=ET.fromstring(z.read(target)); rows=[]
        for row in root.findall('.//'+XLSX_NS+'row'):
            vals={}
            for cell in row.findall(XLSX_NS+'c'):
                col=xlsx_colnum(cell.attrib.get('r','')); typ=cell.attrib.get('t'); v=cell.find(XLSX_NS+'v'); val=''
                if typ=='inlineStr': val=''.join(t.text or '' for t in cell.iter(XLSX_NS+'t'))
                elif v is not None:
                    raw=v.text or ''
                    if typ=='s':
                        try: val=shared[int(raw)]
                        except Exception: val=raw
                    elif typ=='b': val='TRUE' if raw=='1' else 'FALSE'
                    else: val=raw
                vals[col]=val
            if vals:
                maxc=max(vals); rows.append([vals.get(i,'') for i in range(1,maxc+1)])
        out[sh.attrib['name']]=rows
    return out

def multipart_data(h):
    n=int(h.headers.get('Content-Length','0')); raw=h.rfile.read(n); ctype=h.headers.get('Content-Type','')
    if 'multipart/form-data' not in ctype: return {},None,None
    msg=BytesParser(policy=policy.default).parsebytes(b'Content-Type: '+ctype.encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+raw)
    fields={}; file_name=None; file_data=None
    for part in msg.iter_parts():
        name=part.get_param('name', header='content-disposition')
        fn=part.get_filename()
        if fn:
            file_name=fn; file_data=part.get_payload(decode=True)
        elif name:
            payload=part.get_payload(decode=True) or b''; fields[name]=payload.decode('utf-8','ignore')
    return fields,file_name,file_data

def row_dict(headers,row):
    vals=list(row)+['']*max(0,len(headers)-len(row)); return {h:str(vals[i]).strip() for i,h in enumerate(headers)}

def required_sheet(data,name,headers):
    rows=data.get(name)
    if rows is None: return [], [f'Missing sheet: {name}']
    if not rows: return [], [f'Sheet {name} is empty.']
    got=[str(x).strip() for x in rows[0]]
    if got!=headers: return [], [f'{name}: headers do not match template. Expected: {", ".join(headers)}']
    return [row_dict(headers,r) for r in rows[1:] if any(str(x).strip() for x in r)], []

def validate_legacy(data,c):
    specs={'Branches':['Branch_Code','Branch_Name','Address','Manager'],'Customers':['Customer_Code','Customer_Name','Branch_Code','Mobile','WhatsApp','Address','Notes'],'Loans':['Loan_Ref','Customer_Code','Loan_Amount','Interest_Type','Interest_Rate','Total_Payable','Start_Date','Duration_Days','Frequency','Status','Loan_Type','Parent_Loan_Ref','Opening_Balance','New_Cash_Amount'],'Collections':['Payment_Ref','Loan_Ref','Payment_Date','Amount','Mode','Reference','Notes'],'Expenses':['Expense_Ref','Expense_Date','Branch_Code','Category','Amount','Mode','Description','Employee','Frequency']}
    result={}; errors=[]
    for name,headers in specs.items(): result[name],errs=required_sheet(data,name,headers); errors+=errs
    if errors: return result,errors
    cust_codes=set(); loan_refs=set(); pay_refs=set(); exp_refs=set()
    existing_cust={x['legacy_code'] for x in c.execute('SELECT legacy_code FROM customers WHERE legacy_code IS NOT NULL').fetchall()}
    existing_loan={x['legacy_ref'] for x in c.execute('SELECT legacy_ref FROM loans WHERE legacy_ref IS NOT NULL').fetchall()}
    for i,r in enumerate(result['Customers'],2):
        code=r['Customer_Code']; name=r['Customer_Name']
        if not code or not name: errors.append(f'Customers row {i}: Customer_Code and Customer_Name are required.')
        if code in cust_codes: errors.append(f'Customers row {i}: duplicate Customer_Code {code}.')
        cust_codes.add(code)
    for i,r in enumerate(result['Loans'],2):
        ref=r['Loan_Ref']; cc=r['Customer_Code']
        if not ref or not cc: errors.append(f'Loans row {i}: Loan_Ref and Customer_Code are required.')
        if ref in loan_refs: errors.append(f'Loans row {i}: duplicate Loan_Ref {ref}.')
        loan_refs.add(ref)
        if cc not in cust_codes and cc not in existing_cust: errors.append(f'Loans row {i}: Customer_Code {cc} not found in Customers sheet or existing database.')
        try:
            if float(r['Loan_Amount'])<=0 or float(r['Total_Payable'])<=0 or int(float(r['Duration_Days']))<=0: raise ValueError
        except: errors.append(f'Loans row {i}: Loan_Amount, Total_Payable and Duration_Days must be positive numbers.')
        try: date.fromisoformat(excel_date(r['Start_Date']))
        except: errors.append(f'Loans row {i}: invalid Start_Date {r["Start_Date"]}.')
        if r['Interest_Type'] not in ('Flat Interest','Percentage','Flat Total','Reducing Balance'): errors.append(f'Loans row {i}: invalid Interest_Type.')
        if r['Frequency'] not in ('Daily','Weekly','Monthly'): errors.append(f'Loans row {i}: invalid Frequency.')
        if r['Status'] not in ('ACTIVE','CLOSED','ROLLED_OVER'): errors.append(f'Loans row {i}: invalid Status.')
        if r['Loan_Type'] not in ('ORIGINAL','TOPUP'): errors.append(f'Loans row {i}: Loan_Type must be ORIGINAL or TOPUP.')
    for i,r in enumerate(result['Collections'],2):
        ref=r['Payment_Ref']; lr=r['Loan_Ref']
        if not ref or not lr: errors.append(f'Collections row {i}: Payment_Ref and Loan_Ref are required.')
        if ref in pay_refs: errors.append(f'Collections row {i}: duplicate Payment_Ref {ref}.')
        pay_refs.add(ref)
        if lr not in loan_refs and lr not in existing_loan: errors.append(f'Collections row {i}: Loan_Ref {lr} not found in Loans sheet or existing database.')
        try:
            date.fromisoformat(excel_date(r['Payment_Date']))
            if float(r['Amount'])<=0: raise ValueError
        except: errors.append(f'Collections row {i}: Payment_Date and Amount are invalid.')
        if r['Mode'] not in ('Cash','UPI','Bank','Other'): errors.append(f'Collections row {i}: invalid Mode.')
    for i,r in enumerate(result['Expenses'],2):
        if not r['Expense_Ref'] or not r['Expense_Date'] or not r['Category']: errors.append(f'Expenses row {i}: Expense_Ref, Expense_Date and Category are required.')
        if r['Expense_Ref'] in exp_refs: errors.append(f'Expenses row {i}: duplicate Expense_Ref {r["Expense_Ref"]}.')
        exp_refs.add(r['Expense_Ref'])
        try:
            date.fromisoformat(excel_date(r['Expense_Date']))
            if float(r['Amount'])<=0: raise ValueError
        except: errors.append(f'Expenses row {i}: Expense_Date and Amount are invalid.')
        if r['Mode'] not in ('Cash','UPI','Bank','Other'): errors.append(f'Expenses row {i}: invalid Mode.')
        if r['Frequency'] not in ('One-time','Daily','Monthly'): errors.append(f'Expenses row {i}: invalid Frequency.')
    return result,errors

def rebuild_schedule_paid(c,loan_id):
    c.execute('UPDATE schedules SET paid=0 WHERE loan_id=?',(loan_id,)); pays=c.execute('SELECT payment_date,amount,id FROM payments WHERE loan_id=? ORDER BY payment_date,id',(loan_id,)).fetchall(); sched=c.execute('SELECT * FROM schedules WHERE loan_id=? ORDER BY due_date,id',(loan_id,)).fetchall()
    for p in pays:
        rem=float(p['amount'])
        for s in sched:
            cur=float(s['paid']); need=max(0,float(s['expected'])-cur); add=min(rem,need)
            if add>0: c.execute('UPDATE schedules SET paid=paid+? WHERE id=?',(add,s['id'])); s=c.execute('SELECT paid FROM schedules WHERE id=?',(s['id'],)).fetchone(); rem-=add
            if rem<=0.005: break

def import_legacy(data_bytes):
    data=read_xlsx(data_bytes); c=conn(); rows,errors=validate_legacy(data,c)
    if errors: c.close(); return False,errors,{k:len(v) for k,v in rows.items()}
    counts={'customers_added':0,'customers_existing':0,'loans_added':0,'loans_existing':0,'payments_added':0,'payments_existing':0,'expenses_added':0,'expenses_existing':0}
    try:
        brmap={r['code']:r['id'] for r in c.execute('SELECT code,id FROM branches').fetchall()}
        for r in rows['Branches']:
            code=r['Branch_Code'].upper()
            if code not in brmap:
                cur=c.execute('INSERT INTO branches(code,name,address,manager) VALUES(?,?,?,?)',(code,r['Branch_Name'],r['Address'],r['Manager'])); brmap[code]=cur.lastrowid; audit(c,'IMPORT','BRANCH',cur.lastrowid,f'code={code}')
        custmap={r['legacy_code']:r['id'] for r in c.execute('SELECT legacy_code,id FROM customers WHERE legacy_code IS NOT NULL').fetchall()}
        for r in rows['Customers']:
            if r['Customer_Code'] in custmap: counts['customers_existing']+=1; continue
            cur=c.execute('INSERT INTO customers(name,mobile,whatsapp,address,notes,branch_id,legacy_code) VALUES(?,?,?,?,?,?,?)',(r['Customer_Name'],r['Mobile'],r['WhatsApp'],r['Address'],r['Notes'],brmap[r['Branch_Code'].upper()],r['Customer_Code'])); custmap[r['Customer_Code']]=cur.lastrowid; counts['customers_added']+=1; audit(c,'IMPORT','CUSTOMER',cur.lastrowid,f'legacy_code={r["Customer_Code"]}')
        loanmap={r['legacy_ref']:r['id'] for r in c.execute('SELECT legacy_ref,id FROM loans WHERE legacy_ref IS NOT NULL').fetchall()}
        for r in rows['Loans']:
            if r['Loan_Ref'] in loanmap: counts['loans_existing']+=1; continue
            cc=custmap[r['Customer_Code']]; cust=c.execute('SELECT branch_id FROM customers WHERE id=?',(cc,)).fetchone(); bid=cust['branch_id']; amount=float(r['Loan_Amount']); total=float(r['Total_Payable']); days=int(float(r['Duration_Days'])); start=date.fromisoformat(excel_date(r['Start_Date'])); daily=money(total/days); end=start+timedelta(days=days-1); lt=r['Loan_Type'] or 'ORIGINAL'; opening=float(r['Opening_Balance'] or 0); newcash=float(r['New_Cash_Amount'] or 0); cycle=money(opening+newcash) if lt=='TOPUP' else money(amount); parent=None
            if r['Parent_Loan_Ref']:
                parent=loanmap.get(r['Parent_Loan_Ref'])
                if not parent: raise ValueError(f'Parent_Loan_Ref {r["Parent_Loan_Ref"]} must be loaded before child top-up {r["Loan_Ref"]}.')
            cur=c.execute('''INSERT INTO loans(customer_id,branch_id,amount,interest_type,interest_rate,total_payable,daily_due,start_date,end_date,duration_days,frequency,status,loan_type,parent_loan_id,opening_balance,new_cash_amount,cycle_base,legacy_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(cc,bid,amount,r['Interest_Type'],float(r['Interest_Rate'] or 0),total,daily,start.isoformat(),end.isoformat(),days,r['Frequency'],r['Status'],lt,parent,opening,newcash,cycle,r['Loan_Ref']))
            lid=cur.lastrowid; loanmap[r['Loan_Ref']]=lid; counts['loans_added']+=1
            for i in range(days):
                exp=daily if i<days-1 else money(total-daily*(days-1)); c.execute('INSERT INTO schedules(loan_id,due_date,expected,paid) VALUES(?,?,?,0)',(lid,(start+timedelta(days=i)).isoformat(),exp))
            audit(c,'IMPORT','LOAN',lid,f'legacy_ref={r["Loan_Ref"]};amount={amount};total={total}')
        paymap={r['legacy_ref']:r['id'] for r in c.execute('SELECT legacy_ref,id FROM payments WHERE legacy_ref IS NOT NULL').fetchall()}
        for r in rows['Collections']:
            if r['Payment_Ref'] in paymap: counts['payments_existing']+=1; continue
            lid=loanmap[r['Loan_Ref']]; loan=c.execute('SELECT customer_id,branch_id FROM loans WHERE id=?',(lid,)).fetchone(); amt=float(r['Amount']); pd=excel_date(r['Payment_Date'])
            cur=c.execute('INSERT INTO payments(loan_id,customer_id,branch_id,payment_date,amount,mode,reference,notes,legacy_ref) VALUES(?,?,?,?,?,?,?,?,?)',(lid,loan['customer_id'],loan['branch_id'],pd,amt,r['Mode'],r['Reference'],r['Notes'],r['Payment_Ref'])); counts['payments_added']+=1; audit(c,'IMPORT','PAYMENT',cur.lastrowid,f'legacy_ref={r["Payment_Ref"]};loan={r["Loan_Ref"]};amount={amt}')
        for lid in loanmap.values(): rebuild_schedule_paid(c,lid)
        expmap={r['legacy_ref']:r['id'] for r in c.execute('SELECT legacy_ref,id FROM expenses WHERE legacy_ref IS NOT NULL').fetchall()}
        for r in rows['Expenses']:
            if r['Expense_Ref'] in expmap: counts['expenses_existing']+=1; continue
            cur=c.execute('INSERT INTO expenses(branch_id,expense_date,category,amount,mode,description,employee,frequency,legacy_ref) VALUES(?,?,?,?,?,?,?,?,?)',(brmap[r['Branch_Code'].upper()],excel_date(r['Expense_Date']),r['Category'],float(r['Amount']),r['Mode'],r['Description'],r['Employee'],r['Frequency'],r['Expense_Ref'])); counts['expenses_added']+=1; audit(c,'IMPORT','EXPENSE',cur.lastrowid,f'legacy_ref={r["Expense_Ref"]};amount={r["Amount"]}')
        c.commit(); c.close(); return True,[],counts
    except Exception as e:
        c.rollback(); c.close(); return False,[str(e)],counts

def branch_from_request(path):
    v=parse_qs(urlparse(path).query).get('branch',[''])[0]
    return int(v) if v.isdigit() else None

def branch_name(c,bid):
    if not bid: return 'All Branches'
    r=c.execute('SELECT code,name FROM branches WHERE id=?',(bid,)).fetchone()
    return f'{r["code"]} — {r["name"]}' if r else 'All Branches'

def branch_clause(alias,bid):
    return f' AND {alias}.branch_id={int(bid)}' if bid else ''

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self):
        p=urlparse(self.path); path=p.path; q=parse_qs(p.query)
        try:
            routes={'/':self.dashboard,'/customers':self.customers,'/loans':self.loans,'/topups':lambda:self.topups(q.get('loan_id',[''])[0]),'/collections':self.collections,'/outstanding':self.outstanding,'/expenses':self.expenses,'/reports':self.reports,'/audit':self.audit,'/backup':self.backup,'/search':lambda:self.search(q.get('q',[''])[0]),'/flags/3day':lambda:self.flags('3'),'/flags/10day':lambda:self.flags('10'),'/legacy-import':self.legacy_import_page,'/legacy_template.xlsx':self.legacy_template,'/branches':self.branches}
            if path in routes: return routes[path]()
            if path.startswith('/customer/') and path.endswith('/edit'):
                return self.edit_customer_page(int(path.split('/')[-2]))
            if path.startswith('/loan/'): return self.loan_detail(int(path.split('/')[-1]))
            if path=='/export/payments.csv': return self.export_csv('payments')
            if path=='/export/expenses.csv': return self.export_csv('expenses')
            send_html(self,page('404','<div class="panel bad"><h1>404</h1>Page not found.</div>'),404)
        except Exception as e: send_html(self,page('Error',f'<div class="panel bad"><h1>Application error</h1><pre>{escape(str(e))}</pre></div>'),500)
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            if path=='/legacy-import': return self.handle_legacy_import()
            d=form_data(self)
            if path=='/branches': return self.add_branch(d)
            if path=='/customers': return self.add_customer(d)
            if path=='/customers/edit': return self.update_customer(d)
            if path=='/customers/status': return self.set_customer_status(d)
            if path=='/loans': return self.add_loan(d)
            if path=='/collections': return self.add_payment(d)
            if path=='/topups': return self.add_topup(d)
            if path=='/expenses': return self.add_expense(d)
            redirect(self,'/')
        except Exception as e: send_html(self,page('Error',f'<div class="panel bad"><h1>Application error</h1><pre>{escape(str(e))}</pre></div>'),500)

    def dashboard(self):
        c=conn(); bid=branch_from_request(self.path); today=date.today().isoformat(); month=today[:7]; bname=branch_name(c,bid); where=branch_clause('l',bid)
        expected=c.execute(f"SELECT COALESCE(SUM(s.expected),0)x FROM schedules s JOIN loans l ON l.id=s.loan_id WHERE l.status='ACTIVE' AND s.due_date=?{where}",(today,)).fetchone()['x']
        collected=c.execute(f"SELECT COALESCE(SUM(p.amount),0)x FROM payments p JOIN loans l ON l.id=p.loan_id WHERE p.payment_date=?{where}",(today,)).fetchone()['x']
        outstanding=c.execute(f"SELECT COALESCE(SUM(l.total_payable-COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.loan_id=l.id),0)),0)x FROM loans l WHERE l.status='ACTIVE'{where}").fetchone()['x']
        disb=c.execute(f"SELECT COALESCE(SUM(CASE WHEN l.loan_type='TOPUP' THEN l.new_cash_amount ELSE l.amount END),0)x FROM loans l WHERE 1=1{where}").fetchone()['x']
        allcol=c.execute(f"SELECT COALESCE(SUM(p.amount),0)x FROM payments p JOIN loans l ON l.id=p.loan_id WHERE 1=1{where}",( )).fetchone()['x']
        exp_today=c.execute(f"SELECT COALESCE(SUM(e.amount),0)x FROM expenses e WHERE e.expense_date=?{branch_clause('e',bid)}",(today,)).fetchone()['x']
        exp_month=c.execute(f"SELECT COALESCE(SUM(e.amount),0)x FROM expenses e WHERE substr(e.expense_date,1,7)=?{branch_clause('e',bid)}",(month,)).fetchone()['x']
        active=c.execute(f"SELECT COUNT(*)x FROM loans l WHERE l.status='ACTIVE'{where}").fetchone()['x']
        flags3=sum(1 for l in c.execute(f"SELECT l.* FROM loans l WHERE l.status='ACTIVE' AND l.frequency='Daily'{where}").fetchall() if arrears_info(c,l)[0]>=3)
        flags10=sum(1 for l in c.execute(f"SELECT l.* FROM loans l WHERE l.status='ACTIVE' AND l.frequency='Daily'{where}").fetchall() if arrears_info(c,l)[1]>=10)
        rows=c.execute(f"SELECT s.*,l.id loan_id,cu.name FROM schedules s JOIN loans l ON l.id=s.loan_id JOIN customers cu ON cu.id=l.customer_id WHERE l.status='ACTIVE' AND s.due_date=?{where} ORDER BY cu.name",(today,)).fetchall(); c.close()
        shortfall=max(0,money(expected-collected)); pct=(money(collected)/money(expected)*100 if expected else 0); qs=f'?branch={bid}' if bid else ''
        cards=[('Total Amount to Collect',fmt(outstanding),f'/outstanding{qs}'),('Today Expected',fmt(expected),f'/collections{qs}'),('Today Collected',fmt(collected),f'/collections{qs}'),('Today Shortfall',fmt(shortfall),f'/collections{qs}'),('Collection %',f'{pct:.1f}%',f'/collections{qs}'),('3-Day Red Flags',str(flags3),f'/flags/3day{qs}'),('10-Day Monthly Flags',str(flags10),f'/flags/10day{qs}'),('Today Expenses',fmt(exp_today),f'/expenses{qs}')]
        html=''.join(f'<div class="card"><small>{lab}</small><div class="big">{val}</div><a href="{link}">Open</a></div>' for lab,val,link in cards)
        trs=''.join(f'<tr><td>#{r["loan_id"]}</td><td>{escape(r["name"])}</td><td>{r["due_date"]}</td><td>{fmt(r["expected"])}</td><td>{fmt(r["paid"])}</td><td><a class="linkbtn" href="/loan/{r["loan_id"]}">View</a></td></tr>' for r in rows) or '<tr><td colspan="6">No collection due today.</td></tr>'
        body='<h1>Dashboard — '+escape(bname)+'</h1><div class="cards">'+html+'</div><div class="panel"><b>Active loans:</b> '+str(active)+' &nbsp; <b>Total disbursed:</b> '+fmt(disb)+' &nbsp; <b>Total collected:</b> '+fmt(allcol)+' &nbsp; <b>Month expenses:</b> '+fmt(exp_month)+' &nbsp; <b>Net today:</b> '+fmt(collected-exp_today)+'</div><div class="panel"><h2>Today&#39;s Expected Collection vs Actual</h2><div class="tablewrap"><table><tr><th>Loan</th><th>Customer</th><th>Due Date</th><th>Expected</th><th>Allocated Paid</th><th></th></tr>'+trs+'</table></div></div><div class="panel"><h2>Legacy Data Migration</h2><p>Load existing customers, loans, historical collections and expenses from Excel.</p><a class="linkbtn" href="/legacy-import">Open Legacy Load</a> &nbsp; <a href="/legacy_template.xlsx">Download Excel Template</a></div>'
        send_html(self,page('Dashboard',body,bid))

    def branches(self):
        c=conn(); rows=c.execute('SELECT * FROM branches ORDER BY name').fetchall(); c.close()
        trs=''.join(f'<tr><td>{escape(r["code"])}</td><td>{escape(r["name"])}</td><td>{escape(r["address"] or "")}</td><td>{escape(r["manager"] or "")}</td><td>{"Active" if r["active"] else "Inactive"}</td></tr>' for r in rows)
        body=f'<h1>Branch Master</h1><div class="panel"><p>Create and maintain branch master data.</p><form method="post" action="/branches" class="grid"><div><label>Branch Code*</label><input name="code" required></div><div><label>Branch Name*</label><input name="name" required></div><div><label>Address</label><input name="address"><label>Manager</label><input name="manager"><label>&nbsp;</label><button>Add Branch</button></div></form></div><div class="panel"><table><tr><th>Code</th><th>Name</th><th>Address</th><th>Manager</th><th>Status</th></tr>{trs}</table></div>'
        send_html(self,page('Branches',body))

    def add_branch(self,d):
        code=d.get('code','').strip().upper(); name=d.get('name','').strip()
        if not code or not name: return redirect(self,'/branches')
        c=conn(); cur=c.execute('INSERT INTO branches(code,name,address,manager) VALUES(?,?,?,?)',(code,name,d.get('address'),d.get('manager'))); audit(c,'CREATE','BRANCH',cur.lastrowid,f'code={code};name={name}'); c.commit(); c.close(); redirect(self,'/branches')

    def customers(self):
        query=parse_qs(urlparse(self.path).query)
        bid=branch_from_request(self.path)
        status_filter=query.get('status',['all'])[0]
        if status_filter not in ('all','active','inactive'): status_filter='all'
        c=conn(); bname=branch_name(c,bid); where=branch_clause('c',bid)
        status_where=" AND c.active=1" if status_filter=='active' else (" AND c.active=0" if status_filter=='inactive' else "")
        brs=c.execute('SELECT id,code,name FROM branches WHERE active=1 ORDER BY name').fetchall()
        rows=c.execute(f'''SELECT c.*,b.code branch_code FROM customers c
            LEFT JOIN branches b ON b.id=c.branch_id
            WHERE 1=1{where}{status_where} ORDER BY c.active DESC,c.name''').fetchall()
        c.close()
        opts='<option value="" disabled selected>Select branch</option>'+''.join(f'<option value="{r["id"]}">{escape(r["code"])} — {escape(r["name"])}</option>' for r in brs)
        trs=[]
        for r in rows:
            status='Active' if r['active'] else 'Inactive'
            action=(f'<form method="post" action="/customers/status" style="display:inline">'
                    f'<input type="hidden" name="customer_id" value="{r["id"]}">'
                    f'<input type="hidden" name="active" value="{0 if r["active"] else 1}">'
                    f'<button class="{"danger" if r["active"] else ""}" type="submit">{"Deactivate" if r["active"] else "Reactivate"}</button></form>')
            trs.append(f'<tr><td>{escape(r["name"])}</td><td>{escape(r["branch_code"] or "")}</td>'
                       f'<td>{escape(r["mobile"] or "")}</td><td>{escape(r["whatsapp"] or "")}</td>'
                       f'<td>{escape(r["address"] or "")}</td><td>{escape(r["notes"] or "")}</td>'
                       f'<td><b class="{"ok" if r["active"] else "bad"}">{status}</b></td>'
                       f'<td><a class="linkbtn" href="/customer/{r["id"]}/edit">Edit</a> {action}</td></tr>')
        trs=''.join(trs) or '<tr><td colspan="8">No customers match this filter.</td></tr>'
        filter_opts=''.join(f'<option value="{v}" {"selected" if status_filter==v else ""}>{label}</option>' for v,label in (('all','All customers'),('active','Active only'),('inactive','Inactive only')))
        filter_form=f'<form method="get" action="/customers" style="display:flex;gap:10px;align-items:center;max-width:360px"><label style="margin:0">Show status</label><select name="status" onchange="this.form.submit()">{filter_opts}</select>'
        if bid: filter_form+=f'<input type="hidden" name="branch" value="{bid}">'
        filter_form+='</form>'
        body=f'<h1>Customers — {escape(bname)}</h1><div class="panel"><form method="post" action="/customers" class="grid"><div><label>Name*</label><input name="name" required><label>Branch*</label><select name="branch_id" required>{opts}</select></div><div><label>Mobile</label><input name="mobile"><label>WhatsApp</label><input name="whatsapp"></div><div><label>Address</label><input name="address"><label>Notes</label><textarea name="notes"></textarea><label>&nbsp;</label><button>Add Customer</button></div></form><p class="muted">Branch is mandatory. Loans and collections inherit the customer branch.</p></div><div class="panel"><h2>Customer Register</h2>{filter_form}<div class="tablewrap"><table><tr><th>Name</th><th>Branch</th><th>Mobile</th><th>WhatsApp</th><th>Address</th><th>Notes</th><th>Status</th><th>Actions</th></tr>{trs}</table></div></div>'
        send_html(self,page('Customers',body,bid))

    def edit_customer_page(self,cid):
        c=conn()
        customer=c.execute('SELECT * FROM customers WHERE id=?',(cid,)).fetchone()
        if not customer:
            c.close()
            return send_html(self,page('Customer Not Found','<div class="panel bad">Customer not found.</div>'),404)
        branches=c.execute('SELECT id,code,name,active FROM branches ORDER BY active DESC,name').fetchall()
        c.close()
        options=''.join(f'<option value="{b["id"]}" {"selected" if b["id"]==customer["branch_id"] else ""}>{escape(b["code"])} — {escape(b["name"])}{" (Inactive branch)" if not b["active"] else ""}</option>' for b in branches)
        body=f'''<h1>Edit Customer #{cid}</h1><div class="panel">
        <p>Customer status: <b>{"Active" if customer["active"] else "Inactive"}</b></p>
        <form method="post" action="/customers/edit" class="grid">
        <input type="hidden" name="customer_id" value="{cid}">
        <div><label>Name*</label><input name="name" value="{escape(customer["name"] or "",quote=True)}" required>
        <label>Branch*</label><select name="branch_id" required>{options}</select></div>
        <div><label>Mobile</label><input name="mobile" value="{escape(customer["mobile"] or "",quote=True)}">
        <label>WhatsApp</label><input name="whatsapp" value="{escape(customer["whatsapp"] or "",quote=True)}"></div>
        <div><label>Address</label><textarea name="address">{escape(customer["address"] or "")}</textarea>
        <label>Notes</label><textarea name="notes">{escape(customer["notes"] or "")}</textarea>
        <label>&nbsp;</label><button type="submit">Save Customer Changes</button></div>
        </form><p><a href="/customers">Back to Customers</a></p></div>'''
        send_html(self,page('Edit Customer',body))

    def update_customer(self,d):
        cid=int(d.get('customer_id','0') or 0)
        name=d.get('name','').strip()
        try: bid=int(d.get('branch_id','0') or 0)
        except ValueError: bid=0
        if not cid or not name or not bid:
            return send_html(self,page('Invalid Customer','<div class="panel bad">Customer name and branch are required.</div>'),400)
        c=conn()
        old=c.execute('SELECT * FROM customers WHERE id=?',(cid,)).fetchone()
        branch=c.execute('SELECT id FROM branches WHERE id=?',(bid,)).fetchone()
        if not old or not branch:
            c.close()
            return send_html(self,page('Invalid Customer','<div class="panel bad">Customer or selected branch was not found.</div>'),404)
        new_values={'name':name,'branch_id':bid,'mobile':d.get('mobile','').strip(),
                    'whatsapp':d.get('whatsapp','').strip(),'address':d.get('address','').strip(),
                    'notes':d.get('notes','').strip()}
        old_values={key:old[key] for key in new_values}
        changed={key:{'from':old_values[key],'to':new_values[key]}
                 for key in new_values if old_values[key]!=new_values[key]}
        if changed:
            c.execute('''UPDATE customers SET name=?,branch_id=?,mobile=?,whatsapp=?,address=?,notes=?
                         WHERE id=?''',(new_values['name'],new_values['branch_id'],new_values['mobile'],
                         new_values['whatsapp'],new_values['address'],new_values['notes'],cid))
            audit(c,'EDIT','CUSTOMER',cid,json.dumps(changed,ensure_ascii=False,default=str))
        c.commit(); c.close()
        return redirect(self,'/customers')

    def set_customer_status(self,d):
        try:
            cid=int(d.get('customer_id','0') or 0)
            new_status=int(d.get('active','-1'))
        except ValueError:
            return send_html(self,page('Invalid Customer Status','<div class="panel bad">Invalid customer status request.</div>'),400)
        if not cid or new_status not in (0,1):
            return send_html(self,page('Invalid Customer Status','<div class="panel bad">Invalid customer status request.</div>'),400)
        c=conn(); customer=c.execute('SELECT id,name,active FROM customers WHERE id=?',(cid,)).fetchone()
        if not customer:
            c.close()
            return send_html(self,page('Customer Not Found','<div class="panel bad">Customer not found.</div>'),404)
        if int(customer['active'] or 0)!=new_status:
            c.execute('UPDATE customers SET active=? WHERE id=?',(new_status,cid))
            action='REACTIVATE' if new_status else 'DEACTIVATE'
            audit(c,action,'CUSTOMER',cid,f'name={customer["name"]};active={new_status}')
        c.commit(); c.close()
        return redirect(self,'/customers')

    def loans(self):
        c=conn(); bid=branch_from_request(self.path); bname=branch_name(c,bid); where=branch_clause('c',bid)
        customers=c.execute(f'SELECT c.*,b.code branch_code FROM customers c LEFT JOIN branches b ON b.id=c.branch_id WHERE c.active=1{where} ORDER BY c.name').fetchall()
        rows=c.execute(f'''SELECT l.*,c.name,b.code branch_code,COALESCE((SELECT SUM(amount) FROM payments p WHERE p.loan_id=l.id),0) paid FROM loans l JOIN customers c ON c.id=l.customer_id LEFT JOIN branches b ON b.id=c.branch_id WHERE 1=1{where} ORDER BY l.id DESC''').fetchall(); c.close()
        opts=''.join(f'<option value="{r["id"]}">{escape(r["name"])} — {escape(r["branch_code"] or "")} — {escape(r["mobile"] or "")}</option>' for r in customers)
        trs="".join(f"<tr><td><a href='/loan/{r["id"]}'>#{r["id"]}</a></td><td>{escape(r["name"])}</td><td>{escape(r["branch_code"] or "")}</td><td>{r["loan_type"]}</td><td>{fmt(r["amount"] if r["loan_type"]=="ORIGINAL" else r["new_cash_amount"])}</td><td>{fmt(r["total_payable"])}</td><td>{fmt(r["total_payable"]-r["paid"])}</td><td>{r["status"]}</td></tr>" for r in rows)
        js='''<script>function calc(){let a=parseFloat(document.getElementById('amount').value||0),t=document.getElementById('interest_type').value,r=parseFloat(document.getElementById('interest_rate').value||0),x=document.getElementById('total_payable');if(t==='Flat Interest')x.value=(a+r).toFixed(2);else if(t==='Percentage')x.value=(a+a*r/100).toFixed(2);}</script>'''
        body=f'''<h1>Loans — {escape(bname)}</h1><div class="panel"><form method="post" action="/loans" class="grid"><div><label>Customer*</label><select name="customer_id" required>{opts}</select><label>Loan Amount*</label><input id="amount" name="amount" type="number" step="0.01" required oninput="calc()"><label>Interest Type*</label><select id="interest_type" name="interest_type" onchange="calc()"><option>Flat Interest</option><option>Percentage</option><option>Flat Total</option><option>Reducing Balance</option></select></div><div><label>Interest / Rate</label><input id="interest_rate" name="interest_rate" type="number" step="0.01" value="0" oninput="calc()"><label>Total Payable*</label><input id="total_payable" name="total_payable" type="number" step="0.01" required><label>Duration (days)*</label><input name="duration_days" type="number" min="1" required></div><div><label>Start Date*</label><input name="start_date" type="date" value="{date.today().isoformat()}" required><label>Frequency</label><select name="frequency"><option>Daily</option><option>Weekly</option><option>Monthly</option></select><label>&nbsp;</label><button>Create Loan</button><p class="muted">The loan branch is inherited from the selected customer.</p></div></form>{js}</div><div class="panel"><h2>Loan Register</h2><div class="tablewrap"><table><tr><th>Loan</th><th>Customer</th><th>Branch</th><th>Type</th><th>Cash</th><th>Total Payable</th><th>Balance</th><th>Status</th><th></th></tr>{trs}</table></div></div>'''
        send_html(self,page('Loans',body,bid))

    def add_customer(self,d):
        if not d.get('name','').strip() or not d.get('branch_id'): return redirect(self,'/customers')
        bid=int(d['branch_id']); c=conn(); cur=c.execute('INSERT INTO customers(name,mobile,whatsapp,address,notes,branch_id) VALUES(?,?,?,?,?,?)',(d['name'].strip(),d.get('mobile'),d.get('whatsapp'),d.get('address'),d.get('notes'),bid)); audit(c,'CREATE','CUSTOMER',cur.lastrowid,f'{d["name"]};branch_id={bid}'); c.commit(); c.close(); redirect(self,'/customers')

    def add_loan(self,d):
        cid=int(d['customer_id']); amount=parse_amount(d,'amount'); it=d.get('interest_type','Flat Interest'); rate=parse_amount(d,'interest_rate'); supplied=parse_amount(d,'total_payable'); days=int(d['duration_days']); start=date.fromisoformat(d['start_date']); total=calc_total(amount,it,rate,supplied)
        if amount<=0 or days<=0 or total<=0: return redirect(self,'/loans')
        daily=money(total/days); end=start+timedelta(days=days-1); c=conn()
        cb=c.execute('SELECT branch_id,active FROM customers WHERE id=?',(cid,)).fetchone()
        if not cb or not cb['active']:
            c.close()
            return send_html(self,page('Inactive Customer','<div class="panel bad">This customer is inactive. Reactivate the customer before creating a new loan.</div>'),409)
        bid=cb['branch_id']
        lid=c.execute('''INSERT INTO loans(customer_id,branch_id,amount,interest_type,interest_rate,total_payable,daily_due,start_date,end_date,duration_days,frequency,loan_type,new_cash_amount,cycle_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(cid,bid,amount,it,rate,total,daily,start.isoformat(),end.isoformat(),days,d.get('frequency','Daily'),'ORIGINAL',amount,amount)).lastrowid
        for i in range(days):
            exp=daily if i<days-1 else money(total-daily*(days-1)); c.execute('INSERT INTO schedules(loan_id,due_date,expected,paid) VALUES(?,?,?,0)',(lid,(start+timedelta(days=i)).isoformat(),exp))
        audit(c,'CREATE','LOAN',lid,f'amount={amount};type={it};rate={rate};total={total};days={days}'); c.commit(); c.close(); redirect(self,'/loans')

    def topups(self,loan_id=''):
        c=conn(); active=c.execute("SELECT l.*,c.name FROM loans l JOIN customers c ON c.id=l.customer_id WHERE l.status='ACTIVE' AND c.active=1 ORDER BY c.name").fetchall(); selected=None
        if loan_id: selected=c.execute("SELECT l.*,c.name FROM loans l JOIN customers c ON c.id=l.customer_id WHERE l.id=? AND l.status='ACTIVE' AND c.active=1",(int(loan_id),)).fetchone()
        history=c.execute("SELECT t.*,c.name,p.id parent_id FROM loans t JOIN customers c ON c.id=t.customer_id LEFT JOIN loans p ON p.id=t.parent_loan_id WHERE t.loan_type='TOPUP' ORDER BY t.id DESC").fetchall(); c.close()
        if selected:
            bal=active_balance(conn(),selected['id'])
            form=f'''<div class="panel"><h2>New Top-Up for #{selected["id"]} — {escape(selected["name"])}</h2><p>Opening outstanding from previous cycle: <span class="calc">{fmt(bal)}</span></p><form method="post" action="/topups" class="grid"><input type="hidden" name="parent_loan_id" value="{selected["id"]}"><div><label>Opening Balance (carried forward)</label><input value="{bal:.2f}" disabled><label>New Top-Up Cash Requested*</label><input name="new_cash_amount" type="number" step="0.01" min="0.01" required></div><div><label>New Total Payable*</label><input name="total_payable" type="number" step="0.01" required><label>Duration (days)*</label><input name="duration_days" type="number" min="1" required></div><div><label>Start Date*</label><input name="start_date" type="date" value="{date.today().isoformat()}" required><label>Interest / note</label><input name="note" placeholder="Optional"><label>&nbsp;</label><button>Create Top-Up Cycle</button></div></form><p class="muted">Cycle base = carried-forward balance + new cash. Daily due = new Total Payable ÷ Duration. Previous loan becomes ROLLED_OVER to avoid double-counting.</p></div>'''
        else:
            form='<div class="panel"><h2>Select an active loan to top-up</h2><div class="tablewrap"><table><tr><th>Loan</th><th>Customer</th><th>Outstanding</th><th>Daily Due</th><th></th></tr>'+''.join(f'<tr><td>#{r["id"]}</td><td>{escape(r["name"])}</td><td>{fmt(active_balance(conn(),r["id"]))}</td><td>{fmt(r["daily_due"])}</td><td><a class="linkbtn" href="/topups?loan_id={r["id"]}">Start Top-Up</a></td></tr>' for r in active)+'</table></div></div>'
        trs=''.join(f'<tr><td>#{r["id"]}</td><td>{escape(r["name"])}</td><td>#{r["parent_id"]}</td><td>{fmt(r["opening_balance"])}</td><td>{fmt(r["new_cash_amount"])}</td><td>{fmt(r["cycle_base"])}</td><td>{fmt(r["total_payable"])}</td><td>{fmt(r["daily_due"])}</td><td>{r["status"]}</td></tr>' for r in history) or '<tr><td colspan="9">No top-ups yet.</td></tr>'
        body=f'<h1>Top-Ups</h1>{form}<div class="panel"><h2>Top-Up History</h2><div class="tablewrap"><table><tr><th>Top-Up</th><th>Customer</th><th>From Loan</th><th>Opening Balance</th><th>New Cash</th><th>Cycle Base</th><th>New Total Payable</th><th>Daily Due</th><th>Status</th></tr>{trs}</table></div></div>'
        send_html(self,page('Top-Ups',body))

    def add_topup(self,d):
        parent=int(d['parent_loan_id']); newcash=parse_amount(d,'new_cash_amount'); total=parse_amount(d,'total_payable'); days=int(d['duration_days']); start=date.fromisoformat(d['start_date']); c=conn()
        old=c.execute('''SELECT l.* FROM loans l JOIN customers cu ON cu.id=l.customer_id WHERE l.id=? AND l.status='ACTIVE' AND cu.active=1''',(parent,)).fetchone()
        if not old or newcash<=0 or total<=0 or days<=0: c.close(); return redirect(self,'/topups')
        opening=active_balance(c,parent); base=money(opening+newcash); daily=money(total/days); end=start+timedelta(days=days-1)
        lid=c.execute('''INSERT INTO loans(customer_id,branch_id,amount,interest_type,interest_rate,total_payable,daily_due,start_date,end_date,duration_days,frequency,status,loan_type,parent_loan_id,opening_balance,new_cash_amount,cycle_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(old['customer_id'],old['branch_id'],newcash,'Top-Up Cycle',money(total-base),total,daily,start.isoformat(),end.isoformat(),days,'Daily','ACTIVE','TOPUP',parent,opening,newcash,base)).lastrowid
        for i in range(days):
            exp=daily if i<days-1 else money(total-daily*(days-1)); c.execute('INSERT INTO schedules(loan_id,due_date,expected,paid) VALUES(?,?,?,0)',(lid,(start+timedelta(days=i)).isoformat(),exp))
        c.execute("UPDATE loans SET status='ROLLED_OVER' WHERE id=?",(parent,)); audit(c,'CREATE','TOPUP',lid,f'parent={parent};opening={opening};new_cash={newcash};base={base};total={total};days={days};note={d.get("note","")}'); audit(c,'STATUS','LOAN',parent,f'ROLLED_OVER into top-up #{lid}'); c.commit(); c.close(); redirect(self,f'/loan/{lid}')

    def collections(self):
        c=conn(); bid=branch_from_request(self.path); bname=branch_name(c,bid); where=branch_clause('l',bid); rows=c.execute(f'''SELECT l.id loan_id,c.name,l.total_payable,l.daily_due,COALESCE(SUM(p.amount),0) paid,b.code branch_code FROM loans l JOIN customers c ON c.id=l.customer_id LEFT JOIN branches b ON b.id=c.branch_id LEFT JOIN payments p ON p.loan_id=l.id WHERE l.status='ACTIVE'{where} GROUP BY l.id ORDER BY c.name''').fetchall(); today=date.today().isoformat(); expected=c.execute(f"SELECT COALESCE(SUM(s.expected),0)x FROM schedules s JOIN loans l ON l.id=s.loan_id WHERE l.status='ACTIVE' AND s.due_date=?{where}",(today,)).fetchone()['x']; collected=c.execute(f"SELECT COALESCE(SUM(p.amount),0)x FROM payments p JOIN loans l ON l.id=p.loan_id WHERE p.payment_date=?{where}",(today,)).fetchone()['x']; c.close()
        opts=''.join(f'<option value="{r["loan_id"]}">#{r["loan_id"]} — {escape(r["name"])} — {escape(r["branch_code"] or "")} — Daily {fmt(r["daily_due"])}</option>' for r in rows); trs=''.join(f'<tr><td>#{r["loan_id"]}</td><td>{escape(r["name"])}</td><td>{escape(r["branch_code"] or "")}</td><td>{fmt(r["total_payable"])}</td><td>{fmt(r["paid"])}</td><td>{fmt(r["total_payable"]-r["paid"])}</td></tr>' for r in rows)
        body=f'<h1>Collections — {escape(bname)}</h1><div class="cards"><div class="card"><small>Today Expected</small><div class="big">{fmt(expected)}</div></div><div class="card"><small>Today Collected</small><div class="big">{fmt(collected)}</div></div><div class="card"><small>Today Shortfall</small><div class="big">{fmt(max(0,expected-collected))}</div></div><div class="card"><small>Collection %</small><div class="big">{(collected/expected*100 if expected else 0):.1f}%</div></div></div><div class="panel"><form method="post" action="/collections" class="grid"><div><label>Loan*</label><select name="loan_id" required>{opts}</select><label>Amount*</label><input name="amount" type="number" step="0.01" min="0.01" required></div><div><label>Payment Date*</label><input name="payment_date" type="date" value="{today}" required><label>Mode</label><select name="mode"><option>Cash</option><option>UPI</option><option>Bank</option><option>Other</option></select></div><div><label>Reference</label><input name="reference"><label>Notes</label><input name="notes"><label>&nbsp;</label><button>Post Payment</button></div></form><p class="muted">Branch is inherited from the loan.</p></div><div class="panel"><h2>Active Loans & Balances</h2><table><tr><th>Loan</th><th>Customer</th><th>Branch</th><th>Total</th><th>Paid</th><th>Balance</th></tr>{trs}</table></div>'
        send_html(self,page('Collections',body,bid))

    def add_payment(self,d):
        lid=int(d['loan_id']); amount=parse_amount(d,'amount'); pdate=d.get('payment_date') or date.today().isoformat(); c=conn(); loan=c.execute('SELECT * FROM loans WHERE id=? AND status=\'ACTIVE\'',(lid,)).fetchone()
        if not loan or amount<=0: c.close(); return redirect(self,'/collections')
        cur=c.execute('INSERT INTO payments(loan_id,customer_id,branch_id,payment_date,amount,mode,reference,notes) VALUES(?,?,?,?,?,?,?,?)',(lid,loan['customer_id'],loan['branch_id'],pdate,amount,d.get('mode','Cash'),d.get('reference'),d.get('notes'))); audit(c,'CREATE','PAYMENT',cur.lastrowid,f'loan={lid};amount={amount};mode={d.get("mode","Cash")}')
        remaining=amount
        sched=c.execute('SELECT * FROM schedules WHERE loan_id=? AND paid<expected ORDER BY due_date,id',(lid,)).fetchall()
        for s in sched:
            add=min(remaining,money(s['expected']-s['paid']))
            if add>0: c.execute('UPDATE schedules SET paid=paid+? WHERE id=?',(add,s['id'])); remaining=money(remaining-add)
            if remaining<=0: break
        totalpaid=c.execute('SELECT COALESCE(SUM(amount),0)x FROM payments WHERE loan_id=?',(lid,)).fetchone()['x']
        if totalpaid>=loan['total_payable']-0.005: c.execute("UPDATE loans SET status='CLOSED' WHERE id=?",(lid,)); audit(c,'STATUS','LOAN',lid,'CLOSED')
        c.commit(); c.close(); redirect(self,'/collections')

    def outstanding(self):
        c=conn(); bid=branch_from_request(self.path); bname=branch_name(c,bid); where=branch_clause('l',bid); rows=c.execute(f'''SELECT l.*,c.name,b.code branch_code,COALESCE((SELECT SUM(amount) FROM payments p WHERE p.loan_id=l.id),0) paid FROM loans l JOIN customers c ON c.id=l.customer_id LEFT JOIN branches b ON b.id=c.branch_id WHERE l.status='ACTIVE'{where} ORDER BY (l.total_payable-paid) DESC''').fetchall(); total=sum(money(r['total_payable']-r['paid']) for r in rows); c.close(); trs=''.join(f'<tr><td>#{r["id"]}</td><td>{escape(r["name"])}</td><td>{escape(r["branch_code"] or "")}</td><td>{r["loan_type"]}</td><td>{fmt(r["total_payable"])}</td><td>{fmt(r["paid"])}</td><td><b>{fmt(r["total_payable"]-r["paid"])}</b></td><td><a class="linkbtn" href="/loan/{r["id"]}">View</a></td></tr>' for r in rows); body=f'<h1>Total Amount to be Collected — {escape(bname)}</h1><div class="cards"><div class="card"><small>Portfolio Outstanding</small><div class="big">{fmt(total)}</div></div><div class="card"><small>Active Loans</small><div class="big">{len(rows)}</div></div></div><div class="panel"><table><tr><th>Loan</th><th>Customer</th><th>Branch</th><th>Type</th><th>Total Payable</th><th>Paid</th><th>Outstanding</th><th></th></tr>{trs}</table></div>'; send_html(self,page('Outstanding',body,bid))

    def expenses(self):
        c=conn(); bid=branch_from_request(self.path); bname=branch_name(c,bid); where=branch_clause('e',bid); brs=c.execute('SELECT id,code,name FROM branches WHERE active=1 ORDER BY name').fetchall(); rows=c.execute(f'SELECT e.*,b.code branch_code FROM expenses e LEFT JOIN branches b ON b.id=e.branch_id WHERE 1=1{where} ORDER BY expense_date DESC,id DESC LIMIT 300').fetchall(); today=date.today().isoformat(); month=today[:7]; td=c.execute(f'SELECT COALESCE(SUM(amount),0)x FROM expenses e WHERE expense_date=?{where}',(today,)).fetchone()['x']; tm=c.execute(f"SELECT COALESCE(SUM(amount),0)x FROM expenses e WHERE substr(expense_date,1,7)=?{where}",(month,)).fetchone()['x']; c.close(); opts='<option value="" disabled selected>Select branch</option>'+''.join(f'<option value="{r["id"]}">{escape(r["code"])} — {escape(r["name"])}</option>' for r in brs); trs=''.join(f'<tr><td>{r["expense_date"]}</td><td>{escape(r["branch_code"] or "")}</td><td>{escape(r["category"])}</td><td>{fmt(r["amount"])}</td><td>{escape(r["mode"] or "")}</td><td>{escape(r["frequency"] or "")}</td><td>{escape(r["employee"] or "")}</td><td>{escape(r["description"] or "")}</td></tr>' for r in rows); body=f'<h1>Expenses — {escape(bname)}</h1><div class="cards"><div class="card"><small>Today Expenses</small><div class="big">{fmt(td)}</div></div><div class="card"><small>This Month Expenses</small><div class="big">{fmt(tm)}</div></div></div><div class="panel"><form method="post" action="/expenses" class="grid"><div><label>Branch*</label><select name="branch_id" required>{opts}</select><label>Date*</label><input name="expense_date" type="date" value="{today}" required><label>Category*</label><input name="category" required></div><div><label>Amount*</label><input name="amount" type="number" step="0.01" min="0.01" required><label>Payment Mode</label><select name="mode"><option>Cash</option><option>UPI</option><option>Bank</option><option>Other</option></select><label>Frequency</label><select name="frequency"><option>One-time</option><option>Daily</option><option>Monthly</option></select></div><div><label>Employee / Collector</label><input name="employee"><label>Description</label><input name="description"><label>&nbsp;</label><button>Add Expense</button></div></form></div><div class="panel"><table><tr><th>Date</th><th>Branch</th><th>Category</th><th>Amount</th><th>Mode</th><th>Frequency</th><th>Employee</th><th>Description</th></tr>{trs}</table></div>'; send_html(self,page('Expenses',body,bid))

    def add_expense(self,d):
        amount=parse_amount(d,'amount'); bid=int(d['branch_id']) if d.get('branch_id','').isdigit() else None
        if not bid or amount<=0 or not d.get('category','').strip(): return redirect(self,'/expenses')
        c=conn(); cur=c.execute('INSERT INTO expenses(branch_id,expense_date,category,amount,mode,description,employee,frequency) VALUES(?,?,?,?,?,?,?,?)',(bid,d.get('expense_date') or date.today().isoformat(),d.get('category','').strip(),amount,d.get('mode','Cash'),d.get('description'),d.get('employee'),d.get('frequency','One-time'))); audit(c,'CREATE','EXPENSE',cur.lastrowid,f'amount={amount};branch_id={bid}'); c.commit(); c.close(); redirect(self,f'/expenses?branch={bid}')

    def flags(self,kind):
        c=conn(); bid=branch_from_request(self.path); bname=branch_name(c,bid); where=branch_clause('l',bid); rows=[]
        for l in c.execute(f"SELECT l.*,c.name,b.code branch_code FROM loans l JOIN customers c ON c.id=l.customer_id LEFT JOIN branches b ON b.id=c.branch_id WHERE l.status='ACTIVE' AND l.frequency='Daily'{where} ORDER BY c.name").fetchall():
            st,m=arrears_info(c,l)
            if (kind=='3' and st>=3) or (kind=='10' and m>=10): rows.append((l,st,m))
        c.close(); title='3-Day Red Flags' if kind=='3' else '10-Day Monthly Red Flags'; desc='Active daily loans with 3 or more consecutive scheduled days behind at end-of-day.' if kind=='3' else f'Active daily loans with 10 or more overdue/missed collection days in calendar month {date.today():%B %Y}. Days do not need to be consecutive.'; trs=''.join(f'<tr><td>#{l["id"]}</td><td>{escape(l["name"])}</td><td>{escape(l["branch_code"] or "")}</td><td>{fmt(l["daily_due"])}</td><td>{st}</td><td>{m}</td><td><a class="linkbtn danger" href="/loan/{l["id"]}">Open Loan</a></td></tr>' for l,st,m in rows) or '<tr><td colspan="7" class="ok">No current flags.</td></tr>'; body=f'<h1>{title} — {escape(bname)}</h1><div class="panel warn">{desc}</div><div class="panel"><table><tr><th>Loan</th><th>Customer</th><th>Branch</th><th>Daily Due</th><th>Current 3-Day Streak</th><th>Overdue Days This Month</th><th></th></tr>{trs}</table></div>'; send_html(self,page(title,body,bid))

    def loan_detail(self,lid):
        c=conn(); loan=c.execute('SELECT l.*,c.name,c.mobile,c.whatsapp,c.address,b.code branch_code,b.name branch_name FROM loans l JOIN customers c ON c.id=l.customer_id LEFT JOIN branches b ON b.id=c.branch_id WHERE l.id=?',(lid,)).fetchone(); sched=c.execute('SELECT * FROM schedules WHERE loan_id=? ORDER BY due_date',(lid,)).fetchall(); pays=c.execute('SELECT * FROM payments WHERE loan_id=? ORDER BY payment_date DESC,id DESC',(lid,)).fetchall(); parent=c.execute('SELECT id FROM loans WHERE id=?',(loan['parent_loan_id'],)).fetchone() if loan and loan['parent_loan_id'] else None; children=c.execute('SELECT id,status FROM loans WHERE parent_loan_id=? ORDER BY id',(lid,)).fetchall() if loan else []; c.close()
        if not loan: return send_html(self,page('Not Found','<div class="panel bad">Loan not found.</div>'),404)
        paid=sum(x['amount'] for x in pays); bal=money(loan['total_payable']-paid); trs=''.join(f'<tr><td>{s["due_date"]}</td><td>{fmt(s["expected"])}</td><td>{fmt(s["paid"])}</td><td>{"PAID" if s["paid"]>=s["expected"]-0.005 else "PENDING"}</td></tr>' for s in sched); ptrs=''.join(f'<tr><td>{p["payment_date"]}</td><td>{fmt(p["amount"])}</td><td>{escape(p["mode"] or "")}</td><td>{escape(p["reference"] or "")}</td></tr>' for p in pays)
        topup=f'<p><b>Top-Up:</b> opening balance {fmt(loan["opening_balance"])} + new cash {fmt(loan["new_cash_amount"])} = cycle base {fmt(loan["cycle_base"])}. '+(f'Parent loan: <a href="/loan/{parent["id"]}">#{parent["id"]}</a>.' if parent else '')+'</p>' if loan['loan_type']=='TOPUP' else ''
        childtxt=''.join(f'<a href="/loan/{r["id"]}">#{r["id"]}</a> ({r["status"]}) ' for r in children)
        body=f'<h1>Loan #{lid}</h1><div class="panel"><h2>{escape(loan["name"])} <span class="muted">({escape(loan["branch_code"] or "")} — {escape(loan["branch_name"] or "")})</span></h2><p>Type: <b>{loan["loan_type"]}</b> | Status: <b>{loan["status"]}</b> | Cash amount: <b>{fmt(loan["amount"])}</b> | Total Payable: <b>{fmt(loan["total_payable"])}</b> | Paid: <b>{fmt(paid)}</b> | Balance: <b>{fmt(bal)}</b></p><p>Daily Due: {fmt(loan["daily_due"])} | {loan["start_date"]} → {loan["end_date"]} | Duration {loan["duration_days"]} days</p>{topup}<p>Child top-up cycles: {childtxt or "None"}</p></div><div class="panel"><h2>Schedule</h2><table><tr><th>Date</th><th>Expected</th><th>Paid</th><th>Status</th></tr>{trs}</table></div><div class="panel"><h2>Payments</h2><table><tr><th>Date</th><th>Amount</th><th>Mode</th><th>Reference</th></tr>{ptrs or "<tr><td colspan=\"4\">No payments.</td></tr>"}</table></div>'
        send_html(self,page(f'Loan #{lid}',body))

    def reports(self):
        c=conn(); bid=branch_from_request(self.path); bname=branch_name(c,bid); where=branch_clause('l',bid); today=date.today().isoformat(); month=today[:7]; rows=c.execute(f'''SELECT l.id,c.name,b.code branch_code,l.loan_type,l.amount,l.total_payable,COALESCE(SUM(p.amount),0) paid,l.status FROM loans l JOIN customers c ON c.id=l.customer_id LEFT JOIN branches b ON b.id=c.branch_id LEFT JOIN payments p ON p.loan_id=l.id WHERE 1=1{where} GROUP BY l.id ORDER BY l.id DESC''').fetchall(); daily=c.execute(f"SELECT COALESCE(SUM(s.expected),0)x FROM schedules s JOIN loans l ON l.id=s.loan_id WHERE l.status='ACTIVE' AND s.due_date=?{where}",(today,)).fetchone()['x']; monthlycol=c.execute(f"SELECT COALESCE(SUM(p.amount),0)x FROM payments p JOIN loans l ON l.id=p.loan_id WHERE substr(p.payment_date,1,7)=?{where}",(month,)).fetchone()['x']; monthlyexp=c.execute(f"SELECT COALESCE(SUM(e.amount),0)x FROM expenses e WHERE substr(e.expense_date,1,7)=?{branch_clause('e',bid)}",(month,)).fetchone()['x']; c.close(); trs=''.join(f'<tr><td>#{r["id"]}</td><td>{escape(r["name"])}</td><td>{escape(r["branch_code"] or "")}</td><td>{r["loan_type"]}</td><td>{fmt(r["amount"])}</td><td>{fmt(r["total_payable"])}</td><td>{fmt(r["paid"])}</td><td>{fmt(r["total_payable"]-r["paid"])}</td><td>{r["status"]}</td></tr>' for r in rows); body=f'<h1>Reports — {escape(bname)}</h1><div class="cards"><div class="card"><small>Today Expected</small><div class="big">{fmt(daily)}</div></div><div class="card"><small>Month Collections</small><div class="big">{fmt(monthlycol)}</div></div><div class="card"><small>Month Expenses</small><div class="big">{fmt(monthlyexp)}</div></div><div class="card"><small>Month Net Cash</small><div class="big">{fmt(monthlycol-monthlyexp)}</div></div></div><div class="panel"><table><tr><th>Loan</th><th>Customer</th><th>Branch</th><th>Type</th><th>Cash</th><th>Total</th><th>Paid</th><th>Balance</th><th>Status</th></tr>{trs}</table></div>'; send_html(self,page('Reports',body,bid))

    def audit(self):
        c=conn(); rows=c.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 300').fetchall(); c.close(); trs=''.join(f'<tr><td>{r["created_at"]}</td><td>{r["action"]}</td><td>{r["entity"]}</td><td>{r["entity_id"] or ""}</td><td>{escape(r["details"] or "")}</td></tr>' for r in rows); send_html(self,page('Audit',f'<h1>Audit Log</h1><div class="panel"><table><tr><th>Time</th><th>Action</th><th>Entity</th><th>ID</th><th>Details</th></tr>{trs}</table></div>'))

    def legacy_template(self):
        fp=os.path.join(BASE,'MicroLoanManager_Legacy_Load_Template.xlsx')
        if not os.path.exists(fp): return send_html(self,page('Template Missing','<div class="panel bad">The Excel template is missing from the application folder.</div>'),404)
        data=open(fp,'rb').read(); self.send_response(200); self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'); self.send_header('Content-Disposition','attachment; filename="MicroLoanManager_Legacy_Load_Template.xlsx"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)

    def legacy_import_page(self, result=None):
        msg=''
        if result:
            ok,errors,counts=result
            if ok:
                added=sum(v for k,v in counts.items() if k.endswith('_added')); existing=sum(v for k,v in counts.items() if k.endswith('_existing'))
                rows_html=''.join(f'<tr><td>{k.title()}</td><td>{counts.get(k+"_added",0)}</td><td>{counts.get(k+"_existing",0)}</td></tr>' for k in ('customers','loans','payments','expenses'))
                msg=f'<div class="panel ok"><h2>Legacy load completed successfully</h2><p>New records loaded: <b>{added}</b>. Existing records skipped: <b>{existing}</b>.</p><table><tr><th>Area</th><th>Added</th><th>Already Present</th></tr>{rows_html}</table></div>'
            else: msg='<div class="panel bad"><h2>Load not imported</h2><p>The workbook was not changed because validation failed.</p><ul>'+''.join(f'<li>{escape(e)}</li>' for e in errors[:100])+'</ul></div>'
        body=f'''<h1>Legacy Data Load</h1>{msg}<div class="panel"><h2>1. Download the template</h2><p>Fill the five sheets: <b>Branches</b>, <b>Customers</b>, <b>Loans</b>, <b>Collections</b> and <b>Expenses</b>. Keep the sheet names and column headers unchanged.</p><a class="linkbtn" href="/legacy_template.xlsx">Download Excel Template</a></div><div class="panel"><h2>2. Upload completed workbook</h2><form method="post" action="/legacy-import" enctype="multipart/form-data"><label>Select Excel file (.xlsx)</label><input type="file" name="legacy_file" accept=".xlsx" required><br><br><button>Validate &amp; Import Legacy Data</button></form><p class="muted"><b>Safety:</b> the application validates the whole workbook first. If validation fails, no records are imported. Re-loading the same legacy references skips existing records instead of creating duplicates.</p></div><div class="panel"><h2>Field rules</h2><ul><li><b>Customer_Code</b> and <b>Loan_Ref</b> are your old-system identifiers.</li><li><b>Payment_Ref</b> and <b>Expense_Ref</b> are used for duplicate protection.</li><li>Dates should be YYYY-MM-DD or real Excel dates.</li><li>For TOPUP loans, use Loan_Type=TOPUP and provide Parent_Loan_Ref where available.</li><li>Historical payments are loaded and allocated against the generated loan schedule by payment date.</li></ul></div>'''
        send_html(self,page('Legacy Data Load',body))

    def handle_legacy_import(self):
        fields,fn,filedata=multipart_data(self)
        if not filedata or not fn.lower().endswith('.xlsx'):
            return self.legacy_import_page((False,['Please select a valid .xlsx Excel workbook.'],{}))
        try: result=import_legacy(filedata)
        except Exception as e: result=(False,[f'Workbook could not be read: {e}'],{})
        return self.legacy_import_page(result)

    def backup(self):
        os.makedirs(os.path.join(BASE,'backups'),exist_ok=True); stamp=datetime.now().strftime('%Y%m%d_%H%M%S'); target=os.path.join(BASE,'backups',f'microloan_backup_{stamp}.db'); shutil.copy2(DB,target); data=open(target,'rb').read(); self.send_response(200); self.send_header('Content-Type','application/octet-stream'); self.send_header('Content-Disposition',f'attachment; filename="{os.path.basename(target)}"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)

    def search(self,q):
        c=conn(); rows=c.execute('''SELECT l.id loan_id,c.name,c.mobile,l.loan_type,l.amount,l.total_payable,COALESCE(SUM(p.amount),0) paid FROM loans l JOIN customers c ON c.id=l.customer_id LEFT JOIN payments p ON p.loan_id=l.id WHERE c.name LIKE ? OR c.mobile LIKE ? OR CAST(l.id AS TEXT)=? GROUP BY l.id ORDER BY l.id DESC''',(f'%{q}%',f'%{q}%',q)).fetchall(); c.close(); trs=''.join(f'<tr><td><a href="/loan/{r["loan_id"]}">#{r["loan_id"]}</a></td><td>{escape(r["name"])}</td><td>{escape(r["mobile"] or "")}</td><td>{r["loan_type"]}</td><td>{fmt(r["amount"])}</td><td>{fmt(r["total_payable"])}</td><td>{fmt(r["total_payable"]-r["paid"])}</td></tr>' for r in rows); send_html(self,page('Search',f'<h1>Search: {escape(q)}</h1><div class="panel"><table><tr><th>Loan</th><th>Customer</th><th>Mobile</th><th>Type</th><th>Amount</th><th>Total</th><th>Balance</th></tr>{trs or "<tr><td colspan=\"7\">No results.</td></tr>"}</table></div>'))

    def export_csv(self,kind):
        c=conn(); out=io.StringIO(); w=csv.writer(out)
        if kind=='payments': rows=c.execute('SELECT p.id,p.payment_date,c.name,p.loan_id,p.amount,p.mode,p.reference,p.notes FROM payments p JOIN customers c ON c.id=p.customer_id ORDER BY p.payment_date,p.id').fetchall(); w.writerow(['id','payment_date','name','loan_id','amount','mode','reference','notes']); [w.writerow(tuple(r)) for r in rows]; name='payments.csv'
        else: rows=c.execute('SELECT id,expense_date,category,amount,mode,description,employee,frequency FROM expenses ORDER BY expense_date,id').fetchall(); w.writerow(['id','expense_date','category','amount','mode','description','employee','frequency']); [w.writerow(tuple(r)) for r in rows]; name='expenses.csv'
        c.close(); data=out.getvalue().encode(); self.send_response(200); self.send_header('Content-Type','text/csv'); self.send_header('Content-Disposition',f'attachment; filename="{name}"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)

# Initialize database when the application is imported by Vercel
if os.environ.get('VERCEL'):
    init_db()

if __name__=='__main__':
    os.makedirs(LOG_DIR,exist_ok=True)
    os.makedirs(BACKUP_DIR,exist_ok=True)
    init_db()
    seed()
    startup_backup()
    display_host='127.0.0.1' if HOST in ('0.0.0.0','') else HOST
    print('============================================================')
    print(' MicroLoan Manager V1.5 - Production Edition')
    print('============================================================')
    print(f' Server listening on: {HOST}:{PORT}')
    print(f' Open from this computer: http://127.0.0.1:{PORT}/')
    print(f' Other branch PCs: http://<SERVER-IP>:{PORT}/')
    print(' Keep this window open while the application is in use.')
    print(' Stop the server with Ctrl+C.')
    print('============================================================')
    log(f'Server started on {HOST}:{PORT}')
    if CONFIG.get('auto_open_browser',False):
        try: webbrowser.open(f'http://{display_host}:{PORT}/')
        except Exception: pass
    try:
        ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
    except KeyboardInterrupt:
        log('Server stopped by user')
        print('\nServer stopped.')
    except Exception as e:
        log(f'Server stopped with error: {e}')
        raise
