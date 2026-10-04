import csv
import hashlib
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, make_response, redirect, render_template, request, session, url_for
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / '.env')
DATABASE_PATH = Path(os.getenv('VOTELEDGER_DATABASE', ROOT_DIR / 'voters.db'))
OTP_TTL_SECONDS = 300
MAX_OTP_ATTEMPTS = 5
app = Flask(__name__, template_folder=str(ROOT_DIR), static_folder=str(ROOT_DIR), static_url_path='')
app.secret_key = os.getenv('FLASK_SECRET_KEY') or secrets.token_hex(32)
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', '')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
SMTP_HOST = os.getenv('SMTP_HOST')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USERNAME = os.getenv('SMTP_USERNAME')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')
SMTP_FROM = os.getenv('SMTP_FROM', SMTP_USERNAME or '')


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def normalize_phone(phone_number):
    raw = str(phone_number or '').strip()
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return '+91' + digits
    if len(digits) == 12 and digits.startswith('91'):
        return '+' + digits
    return '+' + digits if len(digits) > 10 else raw


def hash_value(value):
    return hashlib.sha256(str(value).strip().encode()).hexdigest()


def validate_phone(phone_number):
    digits = ''.join(ch for ch in str(phone_number or '') if ch.isdigit())
    return len(digits) == 10 or (len(digits) == 12 and digits.startswith('91'))


def block_hash(block):
    return hashlib.sha256(json.dumps(block, sort_keys=True).encode()).hexdigest()


def init_db():
    conn = get_db_connection()
    existing_tables = {row['name'] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    if 'votes' in existing_tables:
        vote_columns = {row['name'] for row in conn.execute('PRAGMA table_info(votes)').fetchall()}
        if 'election_id' not in vote_columns:
            conn.execute('ALTER TABLE votes RENAME TO votes_legacy')
    voter_columns = {row['name'] for row in conn.execute('PRAGMA table_info(voters)').fetchall()}
    if 'registered_at' not in voter_columns and voter_columns:
        conn.execute('ALTER TABLE voters ADD COLUMN registered_at TEXT')
        conn.execute('UPDATE voters SET registered_at = CURRENT_TIMESTAMP WHERE registered_at IS NULL')
    if 'voter_hash' not in voter_columns and voter_columns:
        conn.execute('ALTER TABLE voters ADD COLUMN voter_hash TEXT')
        conn.execute('UPDATE voters SET voter_hash = voter_id WHERE voter_hash IS NULL')
    if 'public_key' not in voter_columns and voter_columns:
        conn.execute('ALTER TABLE voters ADD COLUMN public_key TEXT')
    if 'email' not in voter_columns and voter_columns:
        conn.execute('ALTER TABLE voters ADD COLUMN email TEXT')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS voters (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, dob TEXT NOT NULL,
            voter_id TEXT NOT NULL UNIQUE, voter_hash TEXT NOT NULL UNIQUE,
            public_key TEXT, email TEXT, phone_number TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0, registered_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS elections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT,
            start_date TEXT NOT NULL, end_date TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, election_id INTEGER NOT NULL,
            name TEXT NOT NULL, party TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(election_id) REFERENCES elections(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS otp_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, voter_id INTEGER NOT NULL,
            otp_hash TEXT NOT NULL, expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            used INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            FOREIGN KEY(voter_id) REFERENCES voters(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, block_index INTEGER NOT NULL UNIQUE,
            timestamp TEXT NOT NULL, nonce INTEGER NOT NULL, previous_hash TEXT NOT NULL,
            block_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, election_id INTEGER NOT NULL,
            voter_id INTEGER NOT NULL, voter_hash TEXT NOT NULL, candidate_id INTEGER NOT NULL,
            transaction_hash TEXT NOT NULL UNIQUE, block_index INTEGER, created_at TEXT NOT NULL,
            UNIQUE(election_id, voter_id),
            FOREIGN KEY(election_id) REFERENCES elections(id), FOREIGN KEY(voter_id) REFERENCES voters(id),
            FOREIGN KEY(candidate_id) REFERENCES candidates(id)
        );
    ''')
    if not conn.execute('SELECT 1 FROM blocks LIMIT 1').fetchone():
        genesis = {'index': 1, 'timestamp': utc_now(), 'nonce': 1, 'previous_hash': '0'}
        conn.execute('INSERT INTO blocks (block_index, timestamp, nonce, previous_hash, block_hash) VALUES (?, ?, ?, ?, ?)',
                     (1, genesis['timestamp'], 1, '0', block_hash(genesis)))
    conn.commit()
    conn.close()


def seed_demo_data():
    conn = get_db_connection()
    if not conn.execute('SELECT 1 FROM elections LIMIT 1').fetchone():
        today = datetime.now().date()
        conn.execute('INSERT INTO elections (name, description, start_date, end_date, active, created_at) VALUES (?, ?, ?, ?, 1, ?)',
                     ('National Civic Election', 'Demo election for local development.', str(today), str(today + timedelta(days=30)), utc_now()))
        election_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        for name, party in [('Alice Johnson', 'Civic Alliance'), ('Bob Smith', 'Progress Party'), ('Carol Lee', 'Independent')]:
            conn.execute('INSERT INTO candidates (election_id, name, party, created_at) VALUES (?, ?, ?, ?)', (election_id, name, party, utc_now()))
    demo_voters = [('kishor', '2004-10-31', 'HASH_VOTER123', '9347796080', 'Andhra Pradesh'), ('Gautham', '2000-05-14', 'HASH_VOTER456', '8688387396', 'Andhra Pradesh'), ('Nahida', '1998-12-22', 'HASH_VOTER789', '8019627640', 'Andhra Pradesh')]
    for name, dob, voter_id, phone, state in demo_voters:
        phone = normalize_phone(phone)
        if not conn.execute('SELECT 1 FROM voters WHERE voter_id = ? OR phone_number = ?', (voter_id, phone)).fetchone():
            conn.execute('INSERT INTO voters (name, dob, voter_id, voter_hash, public_key, phone_number, state, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (name, dob, voter_id, hash_value(voter_id), uuid4().hex, phone, state, utc_now()))
    conn.commit()
    conn.close()


def chain_rows(conn):
    return [dict(row) for row in conn.execute('SELECT block_index AS "index", timestamp, nonce, previous_hash, block_hash FROM blocks ORDER BY block_index').fetchall()]


def record_block(conn, transaction_hash):
    previous = conn.execute('SELECT * FROM blocks ORDER BY block_index DESC LIMIT 1').fetchone()
    block = {'index': previous['block_index'] + 1, 'timestamp': utc_now(), 'nonce': 1, 'previous_hash': previous['block_hash'], 'transaction_hash': transaction_hash}
    conn.execute('INSERT INTO blocks (block_index, timestamp, nonce, previous_hash, block_hash) VALUES (?, ?, ?, ?, ?)',
                 (block['index'], block['timestamp'], 1, block['previous_hash'], block_hash(block)))
    return block


def generate_otp():
    return f'{secrets.randbelow(1000000):06d}'


def send_sms(phone_number, message):
    if os.getenv('TWILIO_ACCOUNT_SID') and os.getenv('TWILIO_AUTH_TOKEN') and os.getenv('TWILIO_PHONE_NUMBER'):
        try:
            from twilio.rest import Client
            Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN')).messages.create(body=message, from_=os.getenv('TWILIO_PHONE_NUMBER'), to=phone_number)
            return {'gateway': 'twilio', 'status': 'sent'}
        except Exception as exc:
            return {'gateway': 'twilio', 'status': 'failed', 'error': str(exc)}
    return {'gateway': 'demo', 'status': 'simulated', 'message': message}


def send_email(email_address, subject, message):
    if not email_address:
        return {'gateway': 'none', 'status': 'skipped'}
    if not all([SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM]):
        return {'gateway': 'smtp', 'status': 'not_configured'}
    email = EmailMessage()
    email['Subject'] = subject
    email['From'] = SMTP_FROM
    email['To'] = email_address
    email.set_content(message)
    try:
        smtp_client = smtplib.SMTP_SSL if SMTP_PORT == 465 else smtplib.SMTP
        with smtp_client(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            if SMTP_PORT != 465:
                server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(email)
        return {'gateway': 'smtp', 'status': 'sent'}
    except Exception as exc:
        return {'gateway': 'smtp', 'status': 'failed', 'error': str(exc)}


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return view_func(*args, **kwargs)
    return wrapper


init_db()
seed_demo_data()


@app.route('/')
def index():
    return render_template('kish.html')


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        if ADMIN_USERNAME and ADMIN_PASSWORD and data.get('username') == ADMIN_USERNAME and data.get('password') == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            return jsonify({'message': 'Admin login successful.'})
        return jsonify({'error': 'Invalid admin username or password.'}), 401
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))


@app.route('/admin')
@admin_required
def admin():
    return render_template('admin.html')


@app.route('/admin/registry')
@admin_required
def admin_registry():
    return render_template('admin_registry.html')


@app.route('/admin/voters', methods=['POST'])
@admin_required
def register():
    data = request.get_json(silent=True) or {}
    required = ['name', 'dob', 'voter_id', 'phone_number', 'state']
    if any(not str(data.get(field, '')).strip() for field in required):
        return jsonify({'error': 'Name, date of birth, Voter ID, mobile number, and state are required.'}), 400
    if not validate_phone(data['phone_number']):
        return jsonify({'error': 'Enter a valid Indian mobile number.'}), 400
    email = str(data.get('email') or '').strip().lower()
    if email and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        return jsonify({'error': 'Enter a valid email address or leave it empty.'}), 400
    phone = normalize_phone(data['phone_number'])
    voter_id = str(data['voter_id']).strip()
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO voters (name, dob, voter_id, voter_hash, public_key, email, phone_number, state, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 (str(data['name']).strip(), data['dob'], voter_id, hash_value(voter_id), uuid4().hex, email or None, phone, str(data['state']).strip(), utc_now()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'That voter ID or mobile number is already registered.'}), 409
    voter = conn.execute('SELECT id, voter_id, voter_hash, public_key, email, registered_at FROM voters WHERE voter_id = ?', (voter_id,)).fetchone()
    conn.close()
    return jsonify({'message': 'Voter registered successfully by admin.', **dict(voter)}), 201


@app.route('/admin/voters/<int:voter_id>', methods=['DELETE'])
@admin_required
def delete_voter(voter_id):
    conn = get_db_connection()
    row = conn.execute('SELECT id FROM voters WHERE id = ?', (voter_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Voter not found.'}), 404
    conn.execute('DELETE FROM otp_codes WHERE voter_id = ?', (voter_id,))
    conn.execute('DELETE FROM votes WHERE voter_id = ?', (voter_id,))
    conn.execute('DELETE FROM voters WHERE id = ?', (voter_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Voter removed successfully.'})


@app.route('/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    voter_id = str(data.get('voter_id') or data.get('voter_hash') or '').strip()
    if not voter_id:
        return jsonify({'error': 'Voter ID is required.'}), 400
    conn = get_db_connection()
    voter = conn.execute('SELECT * FROM voters WHERE voter_id = ? OR voter_hash = ?', (voter_id, voter_id)).fetchone()
    if not voter:
        conn.close()
        return jsonify({'error': 'Voter ID is not registered.'}), 404
    supplied_phone = normalize_phone(data.get('phone_number'))
    if supplied_phone and supplied_phone != voter['phone_number']:
        conn.close()
        return jsonify({'error': 'The registered mobile number does not match.'}), 403
    for field in ['name', 'dob', 'state']:
        if data.get(field) and str(data[field]).strip().casefold() != str(voter[field]).strip().casefold():
            conn.close()
            return jsonify({'error': 'The voter information does not match the registry.'}), 403
    otp = generate_otp()
    conn.execute('UPDATE otp_codes SET used = 1 WHERE voter_id = ? AND used = 0', (voter['id'],))
    conn.execute('INSERT INTO otp_codes (voter_id, otp_hash, expires_at, created_at) VALUES (?, ?, ?, ?)',
                 (voter['id'], hash_value(otp), (datetime.now(timezone.utc) + timedelta(seconds=OTP_TTL_SECONDS)).isoformat(), utc_now()))
    conn.commit()
    conn.close()
    sms = send_sms(voter['phone_number'], f'Your VoteLedger OTP is {otp}. It expires in 5 minutes.')
    email_delivery = send_email(voter['email'], 'Your VoteLedger OTP', f'Your VoteLedger OTP is {otp}. It expires in 5 minutes.')
    session['pending_voter_id'] = voter['id']
    destinations = [f'mobile ({voter["phone_number"]})']
    if voter['email']:
        destinations.append(f'email ({voter["email"]})')
    email_status = email_delivery['status']
    message = f'OTP sent to {" and ".join(destinations)}.'
    if voter['email'] and email_status == 'not_configured':
        message += ' Email delivery is not configured; use the demo OTP locally or configure SMTP.'
    elif voter['email'] and email_status == 'failed':
        message += ' Email delivery failed; use the demo OTP locally and check SMTP settings.'
    response = {'message': message, 'voter_id': voter['voter_id'], 'phone_number': voter['phone_number'], 'expires_in': OTP_TTL_SECONDS, 'email_status': email_status}
    if session.get('admin_logged_in'):
        response['demo_otp'] = otp
    return jsonify(response)


@app.route('/verify_otp', methods=['POST'])
def verify_otp():
    data = request.get_json(silent=True) or {}
    voter_id = session.get('pending_voter_id')
    phone = normalize_phone(data.get('phone_number'))
    otp = str(data.get('otp', '')).strip()
    conn = get_db_connection()
    voter = conn.execute('SELECT * FROM voters WHERE id = ? AND phone_number = ?', (voter_id, phone)).fetchone()
    record = conn.execute('SELECT * FROM otp_codes WHERE voter_id = ? AND used = 0 ORDER BY id DESC LIMIT 1', (voter_id,)).fetchone() if voter else None
    if not record or datetime.fromisoformat(record['expires_at']) < datetime.now(timezone.utc):
        conn.close()
        return jsonify({'error': 'OTP is missing or expired. Request a new one.'}), 400
    if record['attempts'] >= MAX_OTP_ATTEMPTS:
        conn.close()
        return jsonify({'error': 'Too many OTP attempts. Request a new one.'}), 429
    if not secrets.compare_digest(record['otp_hash'], hash_value(otp)):
        conn.execute('UPDATE otp_codes SET attempts = attempts + 1 WHERE id = ?', (record['id'],))
        conn.commit()
        conn.close()
        return jsonify({'error': 'Invalid OTP.'}), 400
    conn.execute('UPDATE otp_codes SET used = 1 WHERE id = ?', (record['id'],))
    conn.execute('UPDATE voters SET verified = 1 WHERE id = ?', (voter_id,))
    conn.commit()
    conn.close()
    session['voter_id'] = voter_id
    return jsonify({'message': 'Phone number verified successfully. You can proceed to vote.', 'phone_number': phone})


@app.route('/api/elections')
def elections():
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM elections ORDER BY id DESC').fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['candidates'] = [dict(candidate) for candidate in conn.execute('SELECT id, name, party FROM candidates WHERE election_id = ?', (row['id'],)).fetchall()]
        result.append(item)
    conn.close()
    return jsonify(result)


@app.route('/api/results')
def results():
    conn = get_db_connection()
    elections_rows = conn.execute('SELECT id, name FROM elections ORDER BY id DESC').fetchall()
    payload = []
    for election in elections_rows:
        rows = conn.execute('''
            SELECT c.name AS candidate, c.party, COUNT(v.id) AS votes
            FROM candidates c LEFT JOIN votes v ON v.candidate_id = c.id
            WHERE c.election_id = ? GROUP BY c.id ORDER BY votes DESC, c.name
        ''', (election['id'],)).fetchall()
        total = sum(row['votes'] for row in rows)
        payload.append({'election_id': election['id'], 'election': election['name'], 'total_votes': total,
                        'candidates': [dict(row, percentage=round((row['votes'] / total) * 100, 2) if total else 0) for row in rows]})
    conn.close()
    return jsonify(payload)


@app.route('/vote', methods=['POST'])
def vote():
    data = request.get_json(silent=True) or {}
    voter_id = session.get('voter_id')
    if not voter_id:
        return jsonify({'error': 'Complete voter login and OTP verification first.'}), 403
    conn = get_db_connection()
    voter = conn.execute('SELECT * FROM voters WHERE id = ?', (voter_id,)).fetchone()
    election_id = data.get('election_id')
    election = conn.execute('SELECT * FROM elections WHERE id = ? AND active = 1', (election_id,)).fetchone() if election_id else conn.execute('SELECT * FROM elections WHERE active = 1 ORDER BY id DESC LIMIT 1').fetchone()
    candidate = conn.execute('SELECT * FROM candidates WHERE id = ? AND election_id = ?', (data.get('candidate_id'), election['id'])).fetchone() if election else None
    if not voter or not election or not candidate:
        conn.close()
        return jsonify({'error': 'Choose a valid active election and candidate.'}), 400
    previous_vote = conn.execute('SELECT 1 FROM votes WHERE voter_id = ? LIMIT 1', (voter['id'],)).fetchone()
    if previous_vote:
        conn.close()
        return jsonify({'error': 'You have already used your one vote.'}), 409
    today = datetime.now().date().isoformat()
    if not (election['start_date'] <= today <= election['end_date']):
        conn.close()
        return jsonify({'error': 'This election is not open for voting.'}), 403
    try:
        transaction_hash = hash_value(f'{voter["voter_hash"]}:{election["id"]}:{candidate["id"]}:{secrets.token_hex(16)}')
        block = record_block(conn, transaction_hash)
        conn.execute('INSERT INTO votes (election_id, voter_id, voter_hash, candidate_id, transaction_hash, block_index, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (election['id'], voter['id'], voter['voter_hash'], candidate['id'], transaction_hash, block['index'], utc_now()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({'error': 'You have already used your one vote.'}), 409
    conn.close()
    session.pop('pending_voter_id', None)
    return jsonify({'message': 'Your vote was recorded on the blockchain.', 'block_index': block['index']}), 201


@app.route('/admin/data')
@admin_required
def admin_data():
    conn = get_db_connection()
    voters = conn.execute('SELECT id, name, dob, voter_id, voter_hash, email, phone_number, state, verified, registered_at FROM voters ORDER BY id DESC').fetchall()
    votes = conn.execute('SELECT v.voter_hash, v.transaction_hash, v.block_index, v.created_at, e.name AS election, c.name AS candidate FROM votes v JOIN elections e ON e.id = v.election_id JOIN candidates c ON c.id = v.candidate_id ORDER BY v.id DESC LIMIT 50').fetchall()
    candidates = conn.execute('SELECT c.*, e.name AS election FROM candidates c JOIN elections e ON e.id = c.election_id ORDER BY c.id DESC').fetchall()
    elections_rows = conn.execute('SELECT * FROM elections ORDER BY id DESC').fetchall()
    blocks = chain_rows(conn)
    counts = conn.execute('SELECT c.name AS label, COUNT(v.id) AS value FROM candidates c LEFT JOIN votes v ON v.candidate_id = c.id GROUP BY c.id ORDER BY value DESC').fetchall()
    state_counts = conn.execute('SELECT state AS label, COUNT(*) AS value FROM voters GROUP BY state ORDER BY value DESC').fetchall()
    conn.close()
    total_votes = sum(row['value'] for row in counts)
    candidate_breakdown = [dict(row, percentage=round((row['value'] / total_votes) * 100, 2) if total_votes else 0) for row in counts]
    return jsonify({'stats': {'voters': len(voters), 'verified': sum(row['verified'] for row in voters), 'votes': len(votes), 'blocks': len(blocks), 'elections': len(elections_rows)}, 'voters': [dict(row) for row in voters], 'votes': [dict(row) for row in votes], 'candidates': [dict(row) for row in candidates], 'elections': [dict(row) for row in elections_rows], 'chain': blocks, 'charts': {'candidate_breakdown': candidate_breakdown, 'state_breakdown': [dict(row) for row in state_counts]}})


@app.route('/admin/elections', methods=['GET', 'POST'])
@admin_required
def elections_management():
    if request.method == 'GET':
        return render_template('admin.html')
    data = request.get_json(silent=True) or {}
    if any(not data.get(key) for key in ['name', 'start_date', 'end_date']):
        return jsonify({'error': 'Name, start date, and end date are required.'}), 400
    conn = get_db_connection()
    if data.get('active'):
        conn.execute('UPDATE elections SET active = 0 WHERE active = 1')
    conn.execute('INSERT INTO elections (name, description, start_date, end_date, active, created_at) VALUES (?, ?, ?, ?, ?, ?)', (data['name'], data.get('description', ''), data['start_date'], data['end_date'], int(bool(data.get('active'))), utc_now()))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Election created.'}), 201


@app.route('/admin/elections/<int:election_id>/status', methods=['POST'])
@admin_required
def update_election_status(election_id):
    data = request.get_json(silent=True) or {}
    if 'active' not in data:
        return jsonify({'error': 'Election status is required.'}), 400
    conn = get_db_connection()
    election = conn.execute('SELECT id FROM elections WHERE id = ?', (election_id,)).fetchone()
    if not election:
        conn.close()
        return jsonify({'error': 'Election not found.'}), 404
    if bool(data['active']):
        conn.execute('UPDATE elections SET active = 0 WHERE active = 1')
    conn.execute('UPDATE elections SET active = ? WHERE id = ?', (int(bool(data['active'])), election_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Election activated.' if data['active'] else 'Election completed and closed.'})


@app.route('/admin/elections/clear', methods=['POST'])
@admin_required
def clear_elections():
    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify({'error': 'Confirmation is required to clear all elections.'}), 400
    conn = get_db_connection()
    conn.execute('DELETE FROM votes')
    conn.execute('DELETE FROM candidates')
    deleted = conn.execute('DELETE FROM elections').rowcount
    conn.commit()
    conn.close()
    return jsonify({'message': f'Cleared {deleted} election(s), candidates, and vote records.'})


@app.route('/admin/candidates', methods=['GET', 'POST'])
@admin_required
def candidates_management():
    if request.method == 'GET':
        return render_template('admin.html')
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('party') or not data.get('election_id'):
        return jsonify({'error': 'Candidate name, party, and election are required.'}), 400
    conn = get_db_connection()
    conn.execute('INSERT INTO candidates (election_id, name, party, created_at) VALUES (?, ?, ?, ?)', (data['election_id'], data['name'], data['party'], utc_now()))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Candidate added.'}), 201


@app.route('/admin/candidates/<int:candidate_id>', methods=['PUT'])
@admin_required
def update_candidate(candidate_id):
    data = request.get_json(silent=True) or {}
    if not data.get('name') or not data.get('party'):
        return jsonify({'error': 'Candidate name and party are required.'}), 400
    conn = get_db_connection()
    row = conn.execute('SELECT id FROM candidates WHERE id = ?', (candidate_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Candidate not found.'}), 404
    conn.execute('UPDATE candidates SET name = ?, party = ? WHERE id = ?', (str(data['name']).strip(), str(data['party']).strip(), candidate_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Candidate updated.'})


@app.route('/admin/candidates/<int:candidate_id>', methods=['DELETE'])
@admin_required
def delete_candidate(candidate_id):
    conn = get_db_connection()
    row = conn.execute('SELECT id FROM candidates WHERE id = ?', (candidate_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Candidate not found.'}), 404
    if conn.execute('SELECT 1 FROM votes WHERE candidate_id = ? LIMIT 1', (candidate_id,)).fetchone():
        conn.close()
        return jsonify({'error': 'Cannot remove a candidate after votes have been recorded.'}), 409
    conn.execute('DELETE FROM candidates WHERE id = ?', (candidate_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Candidate and party removed.'})


@app.route('/admin/export/csv')
@admin_required
def admin_export_csv():
    conn = get_db_connection()
    voters = conn.execute('SELECT name, dob, voter_id, voter_hash, public_key, email, phone_number, state, verified FROM voters ORDER BY id').fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['name', 'dob', 'voter_id', 'voter_hash', 'public_key', 'phone_number', 'state', 'verified'])
    writer.writerows([tuple(row) for row in voters])
    response = make_response(output.getvalue())
    response.headers['Content-Disposition'] = 'attachment; filename=voter_registry.csv'
    response.headers['Content-Type'] = 'text/csv'
    return response


@app.route('/chain')
@admin_required
def get_chain():
    conn = get_db_connection()
    chain = chain_rows(conn)
    history = conn.execute('SELECT voter_hash, transaction_hash, block_index, created_at FROM votes ORDER BY id DESC').fetchall()
    conn.close()
    return jsonify({'length': len(chain), 'chain': chain, 'vote_history': [dict(row) for row in history]})


@app.route('/mine')
@admin_required
def mine():
    conn = get_db_connection()
    block = record_block(conn, 'admin-seal')
    conn.commit()
    conn.close()
    return jsonify({'message': 'New block sealed.', **block})


@app.route('/health')
def health():
    conn = get_db_connection()
    blocks = conn.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]
    conn.close()
    return jsonify({'status': 'ok', 'blocks': blocks})


if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG', '0') == '1', host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
