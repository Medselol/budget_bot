"""Persistent screen navigation without replaying actions or undoing saved records."""
import json
import bot as ledger

HOME='Главное меню'
ROOT='Учёт стройки. Выбери действие:'

def init(db):
    db.execute('CREATE TABLE IF NOT EXISTS navigation(user_id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('navigation_revision','0')")
    for table in ('operations','users','accounts','projects','receipts','expense_reviews','budgets','funding_requests','request_files','obligations','site_reports','site_photos','obligation_payments'):
        for action in ('INSERT','UPDATE','DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS nav_{table}_{action.lower()} AFTER {action} ON {table}
                BEGIN UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='navigation_revision'; END''')
    db.commit()

def revision(db):
    return db.execute("SELECT value FROM settings WHERE key='navigation_revision'").fetchone()[0]

class Session:
    def __init__(self,api,db,chat,uid,owner):
        self.api,self.db,self.chat,self.uid,self.owner=api,db,chat,uid,owner
        self.rev=revision(db);self.role=ledger.role_for(db,uid,owner)
        r=db.execute('SELECT data FROM navigation WHERE user_id=?',(uid,)).fetchone()
        state=json.loads(r[0]) if r else {}
        self.pages=state.get('pages',[]) if state.get('revision')==self.rev and state.get('role')==self.role else []
        self.last=None
    def __getattr__(self,key): return getattr(self.api,key)
    def send(self,chat,text,options=None):
        opts=[(HOME if callback=='menu' else label,callback) for label,callback in (options or []) if callback!='nav:back']
        root=text==ROOT
        if not root:
            opts += [('← Назад','nav:back')]
            if not any(c=='menu' for _,c in opts): opts.append((HOME,'menu'))
        self.api.send(chat,text,opts)
        self.last={'text':text,'options':opts,'draft':None,'root':root}
    def finish(self):
        if self.last:
            # A committed change is a navigation boundary. Never restore a saved
            # confirmation draft, even via an older Telegram Back button.
            if revision(self.db)!=self.rev or self.last['root']: self.pages=[]
            self.last['draft']=ledger.draft(self.db,self.uid)
            if not self.pages or self.pages[-1]!=self.last: self.pages.append(self.last)
        state={'revision':revision(self.db),'role':ledger.role_for(self.db,self.uid,self.owner),'pages':self.pages[-20:]}
        with self.db:
            self.db.execute('INSERT INTO navigation(user_id,data) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data',(self.uid,json.dumps(state,ensure_ascii=False)))
    def home(self):
        self.pages=[];ledger.clear_draft(self.db,self.uid)
        ledger.menu(self,self.chat,role=ledger.role_for(self.db,self.uid,self.owner))
    def back(self):
        if len(self.pages)<2: self.home();return
        self.pages.pop();page=self.pages[-1]
        if page['draft'] is None: ledger.clear_draft(self.db,self.uid)
        else: ledger.set_draft(self.db,self.uid,page['draft'])
        self.api.send(self.chat,page['text'],page['options'])
        self.last=None

def dispatch(api,db,chat,uid,owner,value,handler):
    if chat!=uid or not ledger.role_for(db,uid,owner): return
    session=Session(api,db,chat,uid,owner)
    if value in ('menu','cancel','/start','/menu','/cancel','/help'):
        session.home()
    elif value=='nav:back': session.back()
    else:
        from audit_log import actor
        import sqlite3
        with actor(db,uid):
            try: handler(session)
            except sqlite3.IntegrityError as exc:
                db.rollback()
                session.send(chat,str(exc),[('Главное меню','menu')])
    session.finish()
