import os, jwt, csv, io, random, smtplib, requests, json 
import re
from datetime import datetime, timedelta, date
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.background import BackgroundScheduler



load_dotenv()

EMAIL_REGEX = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'

def is_valid_email(email):
    return re.match(EMAIL_REGEX, email) is not None

app = Flask(__name__)

app.config['SECRET_KEY']             = os.environ.get('SECRET_KEY', 'fallback-secret')
db_url = "postgresql://postgres:sQzToICOGpVBvWWzXqpXtoCtyMLwxMfN@shortline.proxy.rlwy.net:26308/railway"
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_SECRET']             = os.environ.get('JWT_SECRET', 'fallback-jwt')
app.config['SESSION_COOKIE_SAMESITE']= 'Lax'

db     = SQLAlchemy(app)
bcrypt = Bcrypt(app)
scheduler = BackgroundScheduler()
scheduler.start()

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'riya_admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'Admin@Secure2025!')
CATEGORIES     = ['Food','Bills','Transport','Shopping','Entertainment','Health','Others']
IDEAL          = {'Food':25,'Bills':20,'Transport':10,'Shopping':15,'Entertainment':10,'Health':10,'Others':10}

# Telegram & Email Config
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_API_URL   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

SES_SMTP_SERVER = os.environ.get('SES_SMTP_SERVER', 'email-smtp.us-west-2.amazonaws.com')
SES_SMTP_PORT   = int(os.environ.get('SES_SMTP_PORT', 587))
SES_SMTP_USER   = os.environ.get('SES_SMTP_USER', '')
SES_SMTP_PASS   = os.environ.get('SES_SMTP_PASSWORD', '')
SES_FROM_EMAIL  = os.environ.get('SES_FROM_EMAIL', 'smartexpense@gmail.com')

# ── Models ────────────────────────────────────────────────────────────────────
class User(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(100), nullable=False)
    email          = db.Column(db.String(120), unique=True, nullable=False)
    password       = db.Column(db.String(200), nullable=False)
    telegram_id    = db.Column(db.String(100), unique=True, nullable=True)
    is_blocked     = db.Column(db.Boolean, default=False)
    is_demo        = db.Column(db.Boolean, default=False)
    monthly_budget = db.Column(db.Float, default=20000.0)
    xp             = db.Column(db.Integer, default=0)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    expenses       = db.relationship('Expense',     backref='user', lazy=True, cascade='all,delete-orphan')
    logs           = db.relationship('ActivityLog', backref='user', lazy=True, cascade='all,delete-orphan')

class Expense(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount      = db.Column(db.Float,   nullable=False)
    category    = db.Column(db.String(50),  nullable=False)
    description = db.Column(db.String(200), nullable=False)
    notes       = db.Column(db.String(500))
    date        = db.Column(db.Date, nullable=False)
    is_surprise = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

class RecurringExpense(db.Model):
    """Recurring expenses - auto-add daily/weekly/monthly"""
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount          = db.Column(db.Float, nullable=False)
    category        = db.Column(db.String(50), nullable=False)
    description     = db.Column(db.String(200), nullable=False)
    frequency       = db.Column(db.String(20), nullable=False)
    start_date      = db.Column(db.Date, nullable=False)
    end_date        = db.Column(db.Date, nullable=True)
    last_added_date = db.Column(db.Date, nullable=True)
    is_active       = db.Column(db.Boolean, default=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref='recurring_expenses')

class ActivityLog(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    action     = db.Column(db.String(100), nullable=False)
    details    = db.Column(db.String(500))
    ip_address = db.Column(db.String(50))
    timestamp  = db.Column(db.DateTime, default=datetime.utcnow)


# ════════════════════════════════════════════════════════════════
# ── HELPER FUNCTIONS  (must be defined BEFORE anything uses them)
# ════════════════════════════════════════════════════════════════

def get_real_ip():
    """Get real user IP — works behind Railway / Render / Vercel proxies"""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'Unknown'

def log_activity(action, user_id=None, details=''):
    """Log any user/admin action to ActivityLog table"""
    try:
        db.session.add(ActivityLog(
            user_id    = user_id,
            action     = action,
            details    = details or '',
            ip_address = get_real_ip()
        ))
        db.session.commit()
    except Exception as e:
        print(f"⚠️ log_activity error: {e}")
        db.session.rollback()

def generate_token(user_id):
    return jwt.encode(
        {'user_id': user_id, 'exp': datetime.utcnow() + timedelta(days=7)},
        app.config['JWT_SECRET'], algorithm='HS256')

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('token') or \
                request.headers.get('Authorization','').replace('Bearer ','')
        if not token:
            return jsonify({'error':'Token missing'}), 401
        try:
            data = jwt.decode(token, app.config['JWT_SECRET'], algorithms=['HS256'])
            user = User.query.get(data['user_id'])
            if not user or user.is_blocked:
                return jsonify({'error':'Access denied'}), 403
        except:
            return jsonify({'error':'Invalid token'}), 401
        return f(user, *args, **kwargs)
    return decorated

def is_surprise(user_id, category, amount):
    if amount > 3000: return True
    past = Expense.query.filter(
        Expense.user_id==user_id, Expense.category==category,
        Expense.date >= date.today()-timedelta(days=90)).all()
    if past and amount > 2*(sum(e.amount for e in past)/len(past)):
        return True
    return False

def get_monthly_totals(user_id, months=4, ref_year=None, ref_month=None):
    results = []
    if ref_year is None or ref_month is None:
        t = date.today(); ref_year, ref_month = t.year, t.month
    for i in range(months-1,-1,-1):
        yr, mo = ref_year, ref_month - i
        while mo <= 0: mo += 12; yr -= 1
        start = date(yr, mo, 1)
        end   = date(yr, mo+1,1)-timedelta(days=1) if mo<12 else date(yr,12,31)
        exps  = Expense.query.filter(
            Expense.user_id==user_id,
            Expense.date>=start, Expense.date<=end).all()
        cats = {c:0.0 for c in CATEGORIES}
        for e in exps: cats[e.category] = cats.get(e.category,0)+e.amount
        results.append({
            'month': start.strftime('%b %Y'), 'year':yr, 'month_num':mo,
            'total': sum(e.amount for e in exps),
            'categories': cats, 'count': len(exps)
        })
    return results

def generate_ai_insights(user_id, ref_year=None, ref_month=None):
    monthly = get_monthly_totals(user_id, 4, ref_year=ref_year, ref_month=ref_month)
    if len(monthly) < 2:
        return ["💡 Kam se kam 2 mahine ka data add karo — phir main smart savings suggestions dunga!"]

    current     = monthly[-1]
    prev_months = monthly[:-1]
    insights    = []
    used_keys   = set()

    cat_avg = {}
    for cat in CATEGORIES:
        vals = [m['categories'][cat] for m in prev_months if m['categories'][cat] > 0]
        cat_avg[cat] = round(sum(vals)/len(vals)) if vals else 0

    # ── 1. Overall trend ──
    prev_totals = [m['total'] for m in prev_months if m['total'] > 0]
    if prev_totals:
        avg_total = sum(prev_totals)/len(prev_totals)
        diff = current['total'] - avg_total
        if diff > avg_total*0.12:
            insights.append(
                f"⚠️ Is mahine ₹{round(current['total']):,} kharch hua — "
                f"pichle {len(prev_months)} mahino ke average "
                f"(₹{round(avg_total):,}) se ₹{round(diff):,} zyada. "
                f"Sabse zyada: {max(CATEGORIES, key=lambda c: current['categories'][c])} category.")
            used_keys.add('overall_high')

    # ── 2. Per-month surplus finder ──
    curr_highs = []
    for cat in CATEGORIES:
        ca = current['categories'][cat]
        av = cat_avg.get(cat, 0)
        if ca > 0 and av > 0 and ca > av * 1.15:
            curr_highs.append((cat, ca, round(ca - av)))
        elif ca > 2500 and av == 0:
            curr_highs.append((cat, ca, round(ca)))
    curr_highs.sort(key=lambda x: x[2], reverse=True)

    for prev_m in prev_months:
        mn = prev_m['month']
        for sav_cat in CATEGORIES:
            av     = cat_avg.get(sav_cat, 0)
            actual = prev_m['categories'][sav_cat]
            if av > 0 and actual > 0 and actual < av * 0.82:
                saved = round(av - actual)
                if saved < 250: continue
                for high_cat, high_amt, extra in curr_highs:
                    if high_cat == sav_cat: continue
                    key = f"cross_{mn}_{sav_cat}_{high_cat}"
                    if key in used_keys: continue
                    used_keys.add(key)
                    cover_pct = min(round(saved/extra*100), 100) if extra > 0 else 0
                    if cover_pct >= 90:
                        insights.append(
                            f"💡 {mn} mein {sav_cat} pe sirf ₹{actual:,} kharch hua tha "
                            f"(average ₹{av:,} hai) — yaani ₹{saved:,} bachaye the! "
                            f"Is mahine {high_cat} mein ₹{extra:,} extra laga — "
                            f"woh {mn} ki bachat se puri tarah cover ho sakta tha.")
                    if len(insights) >= 5: break
                if len(insights) >= 5: break
        if len(insights) >= 5: break

    # ── 3. Best month finder ──
    if len(insights) < 6:
        for cat in CATEGORIES:
            best_m   = min(prev_months, key=lambda m: m['categories'][cat] if m['categories'][cat]>0 else 99999)
            best_amt = best_m['categories'][cat]
            av       = cat_avg.get(cat, 0)
            if best_amt > 0 and av > 0 and best_amt < av * 0.75:
                saved = round(av - best_amt)
                key   = f"best_{cat}"
                if key not in used_keys and saved > 400:
                    used_keys.add(key)
                    future_use = {
                        'Food':       'grocery stocking ya meal prep mein invest karo',
                        'Transport':  'travel ya trip planning mein use karo',
                        'Shopping':   'zaruri cheez ki planning pehle se karo',
                        'Bills':      'advance payment ya savings account mein daalo',
                        'Entertainment': 'special outing ya trip ke liye save karo',
                        'Health':     'preventive checkup ya gym membership ke liye rakho',
                        'Others':     'emergency fund mein add karo',
                    }.get(cat, 'savings mein transfer karo')
                    insights.append(
                        f"🏆 {best_m['month']} mein {cat} ka sabse kam kharch tha "
                        f"(₹{round(best_amt):,} vs avg ₹{av:,}) — "
                        f"₹{saved:,} bache the. Aisi planning agle mahine bhi karo: {future_use}!")
                    if len(insights) >= 6: break

    if not insights:
        insights.append("✨ Is mahine spending quite balanced hai! Aur data add karo for deeper insights.")

    return insights[:7]


# ════════════════════════════════════════════════════════════════
# ── RECURRING EXPENSES PROCESSOR
# ════════════════════════════════════════════════════════════════

def process_recurring_expenses():
    """Auto-add recurring expenses — runs daily at 12 AM via scheduler"""
    today = date.today()
    recurring_list = RecurringExpense.query.filter_by(is_active=True).all()

    for rec in recurring_list:
        if rec.end_date and today > rec.end_date:
            continue

        should_add = False

        if rec.frequency == 'daily':
            should_add = True

        elif rec.frequency == 'weekly':
            if rec.last_added_date is None:
                should_add = True
            elif (today - rec.last_added_date).days >= 7:
                should_add = True

        elif rec.frequency == 'biweekly':
            if rec.last_added_date is None:
                should_add = True
            elif (today - rec.last_added_date).days >= 14:
                should_add = True

        elif rec.frequency == 'monthly':
            if rec.last_added_date is None:
                should_add = True
            elif today.day == rec.start_date.day and (today - rec.last_added_date).days >= 28:
                should_add = True

        if should_add:
            try:
                exp = Expense(
                    user_id     = rec.user_id,
                    amount      = rec.amount,
                    category    = rec.category,
                    description = f"{rec.description} (recurring)",
                    date        = today,
                    is_surprise = False
                )
                db.session.add(exp)
                rec.last_added_date = today
                log_activity('auto_recurring_added', rec.user_id,
                             f"Auto: {rec.description} - ₹{rec.amount}")
                db.session.commit()
                print(f"✅ Recurring added: {rec.description} for user {rec.user_id}")
            except Exception as e:
                print(f"❌ Recurring error: {e}")
                db.session.rollback()


# ── Schedule recurring job AFTER the function is defined ──────────────────────
try:
    from apscheduler.triggers.cron import CronTrigger
    scheduler.add_job(
        process_recurring_expenses,
        trigger=CronTrigger(hour=0, minute=0),
        id='recurring_expenses',
        name='Process recurring expenses',
        replace_existing=True
    )
    print("✅ Scheduler started for recurring expenses")
except Exception as e:
    print(f"⚠️ Scheduler error: {e}")


# ── TELEGRAM FUNCTIONS ────────────────────────────────────────────────────────
def send_telegram_message(chat_id, text):
    """Send message via Telegram Bot API"""
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except:
        pass

def parse_telegram_message(text, user_id):
    """Parse Telegram message and handle commands"""
    text = text.lower().strip()
    u = User.query.get(user_id)
    
    if text.startswith('/start'):
        return f"👋 Namaste {u.name}!\n\nSmartExpense AI mein welcome! Commands:\n/expense food 350 lunch\n/balance\n/summary\n/insights\n/help"
    
    elif text.startswith('/expense '):
        parts = text[9:].split()
        if len(parts) >= 3:
            cat, amt = parts[0], parts[1]
            desc = ' '.join(parts[2:])
            if cat in CATEGORIES:
                try:
                    amt = float(amt)
                    exp = Expense(user_id=user_id, amount=amt, category=cat, 
                                description=desc, date=date.today(), 
                                is_surprise=is_surprise(user_id, cat, amt))
                    db.session.add(exp)
                    u.xp += 10
                    db.session.commit()
                    rem = u.monthly_budget - sum(e.amount for e in Expense.query.filter(
                        Expense.user_id==user_id, 
                        Expense.date>=date(date.today().year, date.today().month, 1)).all())
                    return f"✅ {cat.upper()} ₹{amt:,.0f} '{desc}' added!\n💰 Remaining: ₹{rem:,.0f}\n⚡ XP: +10"
                except:
                    return "❌ Invalid amount"
        return "❌ Format: /expense category amount description\nExample: /expense food 350 lunch"
    
    elif text == '/balance':
        today = date.today()
        start = date(today.year, today.month, 1)
        spent = sum(e.amount for e in Expense.query.filter(
            Expense.user_id==user_id, Expense.date>=start).all())
        rem = u.monthly_budget - spent
        pct = round(spent/u.monthly_budget*100) if u.monthly_budget > 0 else 0
        return f"💰 <b>Monthly Balance</b>\n\n💸 Spent: ₹{spent:,.0f}\n🏦 Budget: ₹{u.monthly_budget:,.0f}\n✅ Remaining: ₹{rem:,.0f}\n📊 Used: {pct}%"
    
    elif text == '/summary':
        today = date.today()
        start = date(today.year, today.month, 1)
        exps = Expense.query.filter(Expense.user_id==user_id, Expense.date>=start).all()
        cats = {c:0.0 for c in CATEGORIES}
        for e in exps: cats[e.category] = cats.get(e.category,0)+e.amount
        summary = "📊 <b>This Month</b>\n\n"
        for cat in sorted(cats.items(), key=lambda x: x[1], reverse=True):
            if cat[1] > 0:
                summary += f"  {cat[0]}: ₹{cat[1]:,.0f}\n"
        summary += f"\n<b>Total: ₹{sum(cats.values()):,.0f}</b>"
        return summary
    
    elif text == '/insights':
        ins = generate_ai_insights(user_id)
        return "🤖 <b>AI Insights</b>\n\n" + "\n\n".join(ins[:3])
    
    elif text == '/help':
        return "📖 <b>Commands</b>\n\n/expense category amount description\n/balance\n/summary\n/insights\n/help"
    
    else:
        return "❓ Command not recognized. Type /help for commands."


# ── EMAIL FUNCTIONS ───────────────────────────────────────────────────────────
def send_email(to_email, subject, html_content):
    """Send email via AWS SES SMTP"""
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = SES_FROM_EMAIL
        msg['To'] = to_email
        msg.attach(MIMEText(html_content, 'html'))
        with smtplib.SMTP(SES_SMTP_SERVER, SES_SMTP_PORT) as server:
            server.starttls()
            server.login(SES_SMTP_USER, SES_SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

def generate_weekly_email(user_id):
    """Generate HTML weekly report email"""
    u = User.query.get(user_id)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    
    exps = Expense.query.filter(
        Expense.user_id==user_id,
        Expense.date>=week_start, Expense.date<=week_end).all()
    
    cats = {c:0.0 for c in CATEGORIES}
    for e in exps: cats[e.category] = cats.get(e.category,0)+e.amount
    total = sum(cats.values())
    
    cat_rows = "".join([
        f"<tr><td style='padding:8px;border-bottom:1px solid #ddd'>{cat}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #ddd;text-align:right'>₹{amt:,.0f}</td></tr>"
        for cat, amt in sorted(cats.items(), key=lambda x: x[1], reverse=True) if amt > 0
    ])
    
    insights = generate_ai_insights(user_id)
    insights_html = "".join([f"<li style='margin-bottom:8px'>{ins}</li>" for ins in insights[:3]])
    
    html = f"""
    <html>
    <body style='font-family:Arial,sans-serif;background:#f5f5f5;padding:20px'>
    <div style='max-width:600px;margin:0 auto;background:white;padding:30px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,0.1)'>
        <h2 style='color:#d63384'>SmartExpense Weekly Report</h2>
        <p style='color:#666'>Week: {week_start.strftime('%b %d')} - {week_end.strftime('%b %d, %Y')}</p>
        
        <div style='margin:20px 0;padding:15px;background:#f9f9f9;border-left:4px solid #d63384'>
            <h3 style='margin:0 0 10px;color:#333'>Weekly Summary</h3>
            <p style='margin:5px 0'><strong>Total Spent:</strong> ₹{total:,.0f}</p>
            <p style='margin:5px 0'><strong>Budget:</strong> ₹{u.monthly_budget:,.0f}</p>
            <p style='margin:5px 0'><strong>Remaining:</strong> ₹{u.monthly_budget - total:,.0f}</p>
        </div>
        
        <h3 style='color:#333'>Category Breakdown</h3>
        <table style='width:100%;border-collapse:collapse'>
            <tr style='background:#f0f0f0'>
                <th style='padding:10px;text-align:left'>Category</th>
                <th style='padding:10px;text-align:right'>Amount</th>
            </tr>
            {cat_rows}
            <tr style='background:#d63384;color:white;font-weight:bold'>
                <td style='padding:10px'>TOTAL</td>
                <td style='padding:10px;text-align:right'>₹{total:,.0f}</td>
            </tr>
        </table>
        
        <div style='margin:20px 0;padding:15px;background:#f0f8ff;border-left:4px solid #0088cc'>
            <h3 style='margin:0 0 10px;color:#0088cc'>🤖 AI Insights</h3>
            <ul style='margin:0;padding-left:20px'>
                {insights_html}
            </ul>
        </div>
        
        <p style='text-align:center;margin-top:30px'>
            <a href='https://smartexpense-ai.vercel.app' style='background:#d63384;color:white;padding:10px 20px;text-decoration:none;border-radius:5px;display:inline-block'>View Full Dashboard</a>
        </p>
        
        <p style='color:#999;font-size:12px;text-align:center;margin-top:20px'>SmartExpense AI • Stay Smart with Your Money</p>
    </div>
    </body>
    </html>
    """
    return html

def send_weekly_emails():
    """Scheduled task — every Sunday 6 PM IST (12:30 PM UTC)"""
    users = User.query.filter(User.telegram_id != None).all()
    for u in users:
        html = generate_weekly_email(u.id)
        send_email(u.email,
                   f"SmartExpense Weekly Report - {date.today().strftime('%b %d, %Y')}",
                   html)

# Schedule weekly emails
try:
    scheduler.add_job(send_weekly_emails, 'cron', day_of_week=6, hour=12, minute=30)
except:
    pass


# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route('/')
def index():
    token = request.cookies.get('token')
    if token:
        try:
            data = jwt.decode(token, app.config['JWT_SECRET'], algorithms=['HS256'])
            u    = User.query.get(data['user_id'])
            if u and not u.is_blocked:
                return redirect(url_for('dashboard'))
        except: pass
    return render_template('index.html')

@app.route('/api/captcha')
def get_captcha():
    a, b = random.randint(1,20), random.randint(1,20)
    op   = random.choice(['+','-','×'])
    if op=='+':   ans=a+b
    elif op=='-': a,b=max(a,b),min(a,b); ans=a-b
    else:         a,b=random.randint(1,10),random.randint(1,10); ans=a*b
    session['captcha_answer'] = ans
    return jsonify({'question': f"{a} {op} {b}"})

@app.route('/api/signup', methods=['POST'])
def signup():
    d = request.get_json()
    if not all([d.get('name'), d.get('email'), d.get('password')]):
        return jsonify({'error':'All fields required'}), 400
    if not is_valid_email(d['email'].lower()):
        return jsonify({'error': 'Invalid email format'}), 400
    if int(d.get('captcha','-1')) != session.get('captcha_answer'):
        return jsonify({'error':'Wrong CAPTCHA answer'}), 400
    if User.query.filter_by(email=d['email'].lower()).first():
        return jsonify({'error':'Email already registered'}), 409
    u = User(name=d['name'], email=d['email'].lower(),
             password=bcrypt.generate_password_hash(d['password']).decode('utf-8'))
    db.session.add(u); db.session.commit()
    log_activity('signup', u.id, f"New user: {u.email}")
    resp = jsonify({'message':'Account created!', 'name':u.name})
    resp.set_cookie('token', generate_token(u.id), httponly=True, max_age=7*24*3600)
    return resp, 201

@app.route('/api/login', methods=['POST'])
def login():
    d = request.get_json()
    if not is_valid_email(d.get('email', '').lower()):
        return jsonify({'error': 'Invalid email format'}), 400
    if int(d.get('captcha','-1')) != session.get('captcha_answer'):
        log_activity('failed_login', details=f"Wrong CAPTCHA: {d.get('email')}")
        return jsonify({'error':'Wrong CAPTCHA answer'}), 400
    u = User.query.filter_by(email=d.get('email','').lower()).first()
    if not u or not bcrypt.check_password_hash(u.password, d.get('password','')):
        log_activity('failed_login', details=f"Bad credentials: {d.get('email')}")
        return jsonify({'error':'Invalid email or password'}), 401
    if u.is_blocked:
        log_activity('blocked_login_attempt', u.id, f"Blocked user tried: {u.email}")
        return jsonify({'error':'Account blocked. Contact support.'}), 403
    log_activity('login', u.id, f"Login: {u.email}")
    resp = jsonify({'message':'Login successful!', 'name':u.name})
    resp.set_cookie('token', generate_token(u.id), httponly=True, max_age=7*24*3600)
    return resp

@app.route('/api/demo-login', methods=['POST'])
def demo_login():
    demo = User.query.filter_by(email='demo@smartexpense.app').first()
    if not demo:
        demo = User(name='Demo User', email='demo@smartexpense.app',
                    password=bcrypt.generate_password_hash('demo1234').decode('utf-8'),
                    is_demo=True, monthly_budget=25000)
        db.session.add(demo); db.session.commit()
        _load_demo(demo.id)
    log_activity('demo_login', demo.id, 'Demo login')
    resp = jsonify({'message':'Demo login!', 'name':demo.name})
    resp.set_cookie('token', generate_token(demo.id), httponly=True, max_age=7*24*3600)
    return resp

@app.route('/api/logout', methods=['POST'])
def logout():
    resp = jsonify({'message':'Logged out'})
    resp.delete_cookie('token')
    return resp

@app.route('/dashboard')
def dashboard():
    token = request.cookies.get('token')
    if not token: return redirect(url_for('index'))
    try:
        data = jwt.decode(token, app.config['JWT_SECRET'], algorithms=['HS256'])
        u    = User.query.get(data['user_id'])
        if not u or u.is_blocked: return redirect(url_for('index'))
    except: return redirect(url_for('index'))
    return render_template('dashboard.html', user=u)


# ── TELEGRAM WEBHOOK ──────────────────────────────────────────────────────────
@app.route('/webhook/telegram', methods=['POST'])
def telegram_webhook():
    try:
        update = request.get_json()
        if 'message' not in update:
            return jsonify({'ok': True})
        msg     = update['message']
        chat_id = msg['chat']['id']
        text    = msg.get('text', '')
        u = User.query.filter_by(telegram_id=str(chat_id)).first()
        if not u:
            send_telegram_message(chat_id, "❌ Please link your Telegram account in Dashboard Settings first!")
            return jsonify({'ok': True})
        response = parse_telegram_message(text, u.id)
        send_telegram_message(chat_id, response)
        return jsonify({'ok': True})
    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({'ok': True})


# ── Expense API ───────────────────────────────────────────────────────────────
@app.route('/api/expenses', methods=['GET'])
@token_required
def get_expenses(u):
    mo    = request.args.get('month', date.today().month, type=int)
    yr    = request.args.get('year',  date.today().year,  type=int)
    start = date(yr, mo, 1)
    end   = date(yr, mo+1,1)-timedelta(days=1) if mo<12 else date(yr,12,31)
    exps  = Expense.query.filter(
        Expense.user_id==u.id, Expense.date>=start, Expense.date<=end
    ).order_by(Expense.date.desc()).all()
    return jsonify([{'id':e.id,'amount':e.amount,'category':e.category,
                     'description':e.description,'notes':e.notes,
                     'date':e.date.isoformat(),'is_surprise':e.is_surprise} for e in exps])

@app.route('/api/expenses', methods=['POST'])
@token_required
def add_expense(u):
    d = request.get_json()
    try:
        amt  = float(d['amount'])
        cat  = d['category']
        desc = d['description']
        dt   = datetime.strptime(d['date'], '%Y-%m-%d').date()
    except (KeyError, ValueError) as e:
        return jsonify({'error':f'Invalid: {e}'}), 400
    surp = is_surprise(u.id, cat, amt)
    exp  = Expense(user_id=u.id, amount=amt, category=cat, description=desc,
                   notes=d.get('notes',''), date=dt, is_surprise=surp)
    db.session.add(exp); u.xp += 10; db.session.commit()
    log_activity('expense_added', u.id, f"{cat}: ₹{amt}")
    return jsonify({'message':'Added!', 'is_surprise':surp, 'xp':u.xp}), 201

@app.route('/api/expenses/<int:eid>', methods=['PUT'])
@token_required
def update_expense(u, eid):
    exp = Expense.query.filter_by(id=eid, user_id=u.id).first_or_404()
    d   = request.get_json()
    exp.amount      = float(d.get('amount',      exp.amount))
    exp.category    = d.get('category',    exp.category)
    exp.description = d.get('description', exp.description)
    exp.notes       = d.get('notes',       exp.notes)
    if 'date' in d: exp.date = datetime.strptime(d['date'],'%Y-%m-%d').date()
    exp.is_surprise = is_surprise(u.id, exp.category, exp.amount)
    db.session.commit()
    log_activity('expense_updated', u.id, f"ID {eid}: {exp.category} ₹{exp.amount}")
    return jsonify({'message':'Updated!'})

@app.route('/api/expenses/<int:eid>', methods=['DELETE'])
@token_required
def delete_expense(u, eid):
    exp = Expense.query.filter_by(id=eid, user_id=u.id).first_or_404()
    log_activity('expense_deleted', u.id, f"ID {eid}: {exp.category} ₹{exp.amount}")
    db.session.delete(exp); db.session.commit()
    return jsonify({'message':'Deleted!'})


# ── Summary & Analytics ───────────────────────────────────────────────────────
@app.route('/api/summary')
@token_required
def get_summary(u):
    mo    = request.args.get('month', date.today().month, type=int)
    yr    = request.args.get('year',  date.today().year,  type=int)
    start = date(yr, mo, 1)
    end   = date(yr, mo+1,1)-timedelta(days=1) if mo<12 else date(yr,12,31)
    exps  = Expense.query.filter(
        Expense.user_id==u.id, Expense.date>=start, Expense.date<=end).all()
    cats  = {c:0.0 for c in CATEGORIES}
    for e in exps: cats[e.category] = cats.get(e.category,0)+e.amount
    total = sum(e.amount for e in exps)
    return jsonify({
        'total':total, 'budget':u.monthly_budget,
        'remaining':u.monthly_budget-total,
        'budget_pct': min((total/u.monthly_budget*100) if u.monthly_budget>0 else 0, 100),
        'categories':cats,
        'surprise_count': sum(1 for e in exps if e.is_surprise),
        'expense_count':  len(exps),
        'xp': u.xp, 'name': u.name
    })

@app.route('/api/insights')
@token_required
def get_insights(u):
    mo = request.args.get('month', date.today().month, type=int)
    yr = request.args.get('year',  date.today().year,  type=int)
    return jsonify({'insights': generate_ai_insights(u.id, ref_year=yr, ref_month=mo),
                    'monthly':  get_monthly_totals(u.id, 4, ref_year=yr, ref_month=mo)})

@app.route('/api/history')
@token_required
def get_history(u):
    months = min(max(request.args.get('months',3,type=int),2),6)
    mo = request.args.get('month', date.today().month, type=int)
    yr = request.args.get('year',  date.today().year,  type=int)
    data   = get_monthly_totals(u.id, months, ref_year=yr, ref_month=mo)
    best   = min(data, key=lambda x:x['total'])['month'] if data else None
    worst  = max(data, key=lambda x:x['total'])['month'] if data else None
    return jsonify({'data':data,'best':best,'worst':worst,
                    'insights': generate_ai_insights(u.id, ref_year=yr, ref_month=mo)})

@app.route('/api/budget', methods=['PUT'])
@token_required
def update_budget(u):
    new_budget = float(request.get_json().get('budget', u.monthly_budget))
    log_activity('budget_updated', u.id, f"₹{u.monthly_budget} → ₹{new_budget}")
    u.monthly_budget = new_budget
    db.session.commit()
    return jsonify({'message':'Budget updated!','budget':u.monthly_budget})

@app.route('/api/telegram/link', methods=['POST'])
@token_required
def link_telegram(user):
    """Link Telegram account"""
    try:
        d = request.get_json()
        telegram_input = d.get('telegram_id', '').strip()
        
        if not telegram_input:
            return jsonify({'error': 'Telegram ID/Username required'}), 400
        
        # अगर username है (@riya_goel28) तो समझाओ कि ID चाहिए
        if telegram_input.startswith('@'):
            return jsonify({
                'error': '❌ Please enter Telegram ID (numbers), not username!\n\nTo get your ID:\n1. Send /getid to bot\n2. Bot will give you the ID\n3. Enter that number here'
            }), 400
        
        # ID को convert करो integer में
        try:
            telegram_id = int(telegram_input)
        except:
            return jsonify({'error': 'Invalid format. Please enter only numbers'}), 400
        
        # User को update करो
        user.telegram_id = telegram_id
        db.session.commit()
        
        # Log करो
        log_activity('telegram_linked', user.id, f"Telegram ID: {telegram_id}")
        
        return jsonify({
            'message': f'✅ Telegram linked! ID: {telegram_id}',
            'telegram_id': telegram_id
        })
    
    except Exception as e:
        print(f"Error: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# ── Demo Data ─────────────────────────────────────────────────────────────────
def _load_demo(user_id):
    Expense.query.filter_by(user_id=user_id).delete()
    db.session.commit()
    today = date.today()
    monthly_data = [
        {'Food':[('Ghar ka khana',800),('Grocery',900),('Chai-snacks',200)],
         'Bills':[('Electricity',1800),('Internet',999)],
         'Transport':[('Metro pass',800),('Auto',300)],
         'Shopping':[('Kapde',1200),('Pharmacy',400)],
         'Entertainment':[('Movie',400),('OTT',499)],
         'Health':[('Gym',1200),('Vitamins',400)],
         'Others':[('Gifts',500)]},
        {'Food':[('Restaurant',2200),('Grocery',1400),('Swiggy',900),('Chai',300)],
         'Bills':[('Electricity',2200),('Internet',999),('Gas',800)],
         'Transport':[('Uber',1200),('Petrol',1800),('Auto',400)],
         'Shopping':[('Amazon',2100),('Myntra',1500)],
         'Entertainment':[('Concerts',1200),('MovieTickets',800),('OTT',499)],
         'Health':[('Doctor',800),('Medicines',600),('Gym',1200)],
         'Others':[('Donation',300),('Misc',600)]},
        {'Food':[('Parties',3200),('Grocery',1800),('Zomato',1400),('Cafe',800)],
         'Bills':[('Electricity',2400),('Internet',999),('Gas',850)],
         'Transport':[('Metro',600),('Auto',200)],
         'Shopping':[('Birthday gifts',3500),('Clothes',1200)],
         'Entertainment':[('OTT',499),('Game',300)],
         'Health':[('Gym',1200),('Checkup',500)],
         'Others':[('Charity',400)]},
    ]
    for offset, m_data in enumerate(reversed(monthly_data)):
        yr, mo = today.year, today.month - (len(monthly_data)-1-offset)
        while mo <= 0: mo += 12; yr -= 1
        for cat, items in m_data.items():
            for desc, base in items:
                amt   = round(base * random.uniform(0.90, 1.10), 2)
                edate = date(yr, mo, random.randint(1,28))
                db.session.add(Expense(user_id=user_id, amount=amt, category=cat,
                                       description=desc, date=edate,
                                       is_surprise=amt>3000))
    db.session.commit()

@app.route('/api/load-demo-data', methods=['POST'])
@token_required
def load_demo_data(u):
    _load_demo(u.id); u.xp = 150; db.session.commit()
    log_activity('demo_data_loaded', u.id, '3 months demo data loaded')
    return jsonify({'message':'3 months of real demo data loaded!'})


# ── Export ────────────────────────────────────────────────────────────────────
@app.route('/api/export/csv')
@token_required
def export_csv(u):
    exps = Expense.query.filter_by(user_id=u.id).order_by(Expense.date.desc()).all()
    out  = io.StringIO(); w = csv.writer(out)
    w.writerow(['Date','Month','Category','Description','Amount','Notes','Surprise'])
    for e in exps:
        w.writerow([e.date, e.date.strftime('%b %Y'), e.category,
                    e.description, e.amount, e.notes or '',
                    'Yes' if e.is_surprise else 'No'])
    out.seek(0)
    log_activity('csv_exported', u.id, f"{len(exps)} expenses exported")
    return make_response(out.getvalue(), 200,
        {'Content-Type':'text/csv',
         'Content-Disposition':f'attachment; filename=expenses_{u.name}.csv'})


# ── ADMIN ──────────────────────────────────────────────────────────────────────
@app.route('/admin')
def admin_page():
    if session.get('admin_logged_in'):
        return render_template('admin.html', already_logged_in=True)
    return render_template('admin.html', already_logged_in=False)

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    d = request.get_json()
    if d.get('username')==ADMIN_USERNAME and d.get('password')==ADMIN_PASSWORD:
        session['admin_logged_in'] = True
        session.permanent = True
        log_activity('admin_login', details=f'Admin login from {get_real_ip()}')
        return jsonify({'success': True})
    log_activity('admin_failed_login', details=f'Failed admin login from {get_real_ip()}')
    return jsonify({'error':'Invalid credentials'}), 401

@app.route('/api/admin/check')
def admin_check():
    return jsonify({'logged_in': bool(session.get('admin_logged_in'))})

@app.route('/api/admin/users')
def admin_users():
    if not session.get('admin_logged_in'): return jsonify({'error':'Unauthorized'}), 401
    return jsonify([{'id':u.id,'name':u.name,'email':u.email,
                     'telegram_id':u.telegram_id,'is_blocked':u.is_blocked,'is_demo':u.is_demo,
                     'expense_count':len(u.expenses),
                     'total_spent':sum(e.amount for e in u.expenses),
                     'created_at':u.created_at.isoformat(),'xp':u.xp}
                    for u in User.query.all()])

@app.route('/api/admin/expenses')
def admin_expenses():
    if not session.get('admin_logged_in'): return jsonify({'error':'Unauthorized'}), 401
    exps = Expense.query.order_by(Expense.date.desc()).limit(300).all()
    return jsonify([{'id':e.id,'user_id':e.user_id,
                     'user_name': (User.query.get(e.user_id).name if User.query.get(e.user_id) else 'Deleted'),
                     'amount':e.amount,'category':e.category,
                     'description':e.description,'date':e.date.isoformat(),
                     'is_surprise':e.is_surprise} for e in exps])

@app.route('/api/admin/logs')
def admin_logs():
    if not session.get('admin_logged_in'): return jsonify({'error':'Unauthorized'}), 401
    return jsonify([{'id':l.id,'user_id':l.user_id,'action':l.action,
                     'details':l.details,'ip':l.ip_address,
                     'timestamp':l.timestamp.isoformat()}
                    for l in ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(200).all()])

@app.route('/api/admin/block/<int:uid>', methods=['POST'])
def admin_block(uid):
    if not session.get('admin_logged_in'): return jsonify({'error':'Unauthorized'}), 401
    u = User.query.get_or_404(uid)
    u.is_blocked = not u.is_blocked; db.session.commit()
    log_activity('admin_block_toggle', details=f"User {uid} → {'blocked' if u.is_blocked else 'unblocked'}")
    return jsonify({'message':f"{'Blocked' if u.is_blocked else 'Unblocked'}!", 'is_blocked':u.is_blocked})

@app.route('/api/admin/delete/<int:uid>', methods=['DELETE'])
def admin_delete(uid):
    if not session.get('admin_logged_in'): return jsonify({'error':'Unauthorized'}), 401
    u = User.query.get_or_404(uid)
    log_activity('admin_delete_user', details=f"Deleted user {uid}: {u.email}")
    db.session.delete(u); db.session.commit()
    return jsonify({'message':'Deleted!'})

@app.route('/api/admin/export')
def admin_export():
    if not session.get('admin_logged_in'): return jsonify({'error':'Unauthorized'}), 401
    exps = Expense.query.order_by(Expense.date.desc()).all()
    out  = io.StringIO(); w = csv.writer(out)
    w.writerow(['User ID','User Name','Date','Month','Category','Description','Amount','Surprise'])
    for e in exps:
        usr = User.query.get(e.user_id)
        w.writerow([e.user_id, usr.name if usr else 'Deleted', e.date,
                    e.date.strftime('%b %Y'), e.category,
                    e.description, e.amount, 'Yes' if e.is_surprise else 'No'])
    out.seek(0)
    return make_response(out.getvalue(), 200,
        {'Content-Type':'text/csv',
         'Content-Disposition':'attachment; filename=all_expenses_admin.csv'})

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    log_activity('admin_logout', details=f'Admin logout from {get_real_ip()}')
    session.pop('admin_logged_in', None)
    return jsonify({'message':'Logged out'})


# ── Recurring Expense Routes ───────────────────────────────────────────────────
@app.route('/api/recurring', methods=['GET'])
@token_required
def get_recurring(u):
    try:
        recurring = RecurringExpense.query.filter_by(user_id=u.id, is_active=True).all()
        return jsonify([{
            'id': r.id,
            'amount': r.amount,
            'category': r.category,
            'description': r.description,
            'frequency': r.frequency,
            'start_date': r.start_date.isoformat(),
            'end_date': r.end_date.isoformat() if r.end_date else None,
            'is_active': r.is_active,
            'last_added_date': r.last_added_date.isoformat() if r.last_added_date else None
        } for r in recurring])
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': 'Failed to fetch'}), 500

@app.route('/api/recurring', methods=['POST'])
@token_required
def add_recurring(u):
    try:
        d    = request.get_json()
        amt  = float(d['amount'])
        cat  = d['category']
        desc = d['description']
        freq = d['frequency']
        start= datetime.strptime(d['start_date'], '%Y-%m-%d').date()
        end  = datetime.strptime(d['end_date'], '%Y-%m-%d').date() if d.get('end_date') else None

        if not all([amt, cat, desc, freq, start]):
            return jsonify({'error': 'Missing required fields'}), 400
        if amt <= 0:
            return jsonify({'error': 'Amount must be > 0'}), 400

        rec = RecurringExpense(user_id=u.id, amount=amt, category=cat,
                               description=desc, frequency=freq,
                               start_date=start, end_date=end, is_active=True)
        db.session.add(rec)
        db.session.commit()
        log_activity('recurring_added', u.id, f"{desc} ({freq}): ₹{amt}")
        return jsonify({
            'message': '✅ Recurring added!',
            'id': rec.id,
            'recurring': {'id':rec.id,'description':rec.description,
                          'amount':rec.amount,'frequency':rec.frequency}
        }), 201
    except ValueError as e:
        return jsonify({'error': f'Invalid data: {str(e)}'}), 400
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': 'Failed to add'}), 500

@app.route('/api/recurring/<int:rid>', methods=['DELETE'])
@token_required
def delete_recurring(u, rid):
    try:
        rec = RecurringExpense.query.filter_by(id=rid, user_id=u.id).first()
        if not rec:
            return jsonify({'error': 'Not found'}), 404
        rec.is_active = False
        db.session.commit()
        log_activity('recurring_deleted', u.id, f"{rec.description}")
        return jsonify({'message': '✅ Deleted!'})
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': 'Failed to delete'}), 500

@app.route('/api/recurring/<int:rid>/toggle', methods=['POST'])
@token_required
def toggle_recurring(u, rid):
    try:
        rec = RecurringExpense.query.filter_by(id=rid, user_id=u.id).first()
        if not rec:
            return jsonify({'error': 'Not found'}), 404
        rec.is_active = not rec.is_active
        db.session.commit()
        status = "resumed" if rec.is_active else "paused"
        log_activity('recurring_toggled', u.id, f"{rec.description} - {status}")
        return jsonify({'message': f'✅ {status.capitalize()}!', 'is_active': rec.is_active})
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'error': 'Failed to toggle'}), 500


# ── Init & Run ────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
