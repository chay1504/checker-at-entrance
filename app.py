import os
import sqlite3
import threading
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, flash, session
import pandas as pd
import qrcode
from io import BytesIO
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect

app = Flask(__name__)
app.secret_key = os.urandom(24)
csrf = CSRFProtect(app)

app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

DB_PATH = 'workshop.db'
db_lock = threading.Lock()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_lock:
        conn = get_db_connection()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS attendees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT,
                college TEXT,
                unique_code TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'unattended',
                check_in_time TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        try:
            conn.execute('CREATE INDEX idx_unique_code ON attendees(unique_code)')
        except sqlite3.OperationalError:
            pass # Index already exists
        conn.commit()
        conn.close()

init_db()

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == 'user' and password == 'password':
            session['admin_logged_in'] = True
            return redirect(url_for('main_portal'))
        else:
            flash('Invalid credentials', 'error')
    return render_template('login.html')

@app.route('/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

@app.route('/', methods=['GET'])
@admin_required
def main_portal():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key='workshop_name'")
        row = cursor.fetchone()
        workshop_name = row['value'] if row else "No Workshop Setup Yet"
        conn.close()
    return render_template('main.html', workshop_name=workshop_name)

@app.route('/student', methods=['GET'])
def student_portal():
    return render_template('student.html')

@app.route('/checkin', methods=['POST'])
def checkin_api():
    code = request.form.get('unique_code', '').strip()
    if not code:
        return jsonify({'success': False, 'message': 'Phone number/Code is required.', 'type': 'error'})
    
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            # Allow checkin via valid unique_code or phone numbers (some duplicates might share phone so unique_code logic is stronger but we support both)
            cursor.execute("SELECT * FROM attendees WHERE unique_code = ? OR phone = ?", (code, code))
            attendee = cursor.fetchone()
            
            if not attendee:
                conn.close()
                return jsonify({'success': False, 'message': 'Not Found. Please try again.', 'type': 'error'})
            
            status = attendee['status']
            name = attendee['name']
            
            if status == 'attended':
                conn.close()
                return jsonify({'success': False, 'message': 'Already Checked In', 'type': 'warning'})
            
            # Update to attended
            current_time = datetime.now()
            cursor.execute(
                "UPDATE attendees SET status = 'attended', check_in_time = ? WHERE id = ?", 
                (current_time, attendee['id'])
            )
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': name, 'type': 'success', 'name': name})
    except Exception as e:
        return jsonify({'success': False, 'message': 'Database Error. Please try again.', 'type': 'error'})

@app.route('/admin/import', methods=['GET', 'POST'])
@admin_required
def admin_import():
    if request.method == 'POST':
        action = request.form.get('action')
        workshop_name = request.form.get('workshop_name')
        if not workshop_name:
            flash('Workshop name is required', 'error')
            return redirect(request.url)
            
        if 'file' not in request.files:
            flash('No file uploaded', 'error')
            return redirect(request.url)
        
        file = request.files['file']
        if file.filename == '':
            flash('No selected file', 'error')
            return redirect(request.url)
            
        if file and file.filename.endswith('.xlsx'):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            try:
                df = pd.read_excel(filepath)
                # Ensure the columns exist
                required_cols = {'Name', 'Phone'}
                if not required_cols.issubset(set(df.columns)):
                    flash('Excel must contain at least: Name, Phone. We will map phone as code as well if Unique_Code is missing.', 'error')
                    os.remove(filepath)
                    return redirect(request.url)
                    
                records_added = 0
                records_failed = 0
                
                with db_lock:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    
                    if action == 'create_new':
                        cursor.execute("DELETE FROM attendees")
                        
                    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('workshop_name', ?)", (workshop_name,))
                    
                    for _, row in df.iterrows():
                        try:
                            # We will use phone as unique code if unique_code missing
                            phone_val = str(row['Phone']).strip() if pd.notna(row.get('Phone')) else ''
                            code_val = str(row['Unique_Code']).strip() if 'Unique_Code' in df.columns and pd.notna(row['Unique_Code']) else phone_val
                            
                            # Avoid empty codes
                            if not code_val:
                                continue

                            # Avoid duplicates internally
                            cursor.execute("SELECT id FROM attendees WHERE unique_code = ?", (code_val,))
                            if not cursor.fetchone():
                                cursor.execute('''
                                    INSERT INTO attendees (name, phone, college, unique_code, status)
                                    VALUES (?, ?, ?, ?, 'unattended')
                                ''', (
                                    str(row['Name']).strip(),
                                    phone_val,
                                    str(row.get('College', '')).strip() if 'College' in df.columns else '',
                                    code_val
                                ))
                                records_added += 1
                        except Exception as e:
                            records_failed += 1
                            continue
                            
                    conn.commit()
                    conn.close()
                    
                flash(f'Successfully imported {records_added} records. {records_failed} duplicates/failures.', 'success')
            except Exception as e:
                flash(f'Error processing file: {str(e)}', 'error')
            finally:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Please upload a valid .xlsx file or enter correct data', 'error')
            
    return render_template('admin_import.html')

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM attendees")
        total_registered = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM attendees WHERE status = 'attended'")
        total_attended = cursor.fetchone()[0]
        
        cursor.execute("SELECT value FROM settings WHERE key='workshop_name'")
        row = cursor.fetchone()
        workshop_name = row['value'] if row else "Create a Workshop First"
        
        conn.close()
        
    return render_template('admin_dashboard.html', 
                           total_registered=total_registered, 
                           total_attended=total_attended,
                           workshop_name=workshop_name)

@app.route('/admin/export')
@admin_required
def admin_export():
    with db_lock:
        conn = get_db_connection()
        df = pd.read_sql_query("SELECT name, phone, college, unique_code, status, check_in_time FROM attendees WHERE status = 'attended'", conn)
        conn.close()
        
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Attended')
        
    output.seek(0)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        output,
        download_name=f'attended_list_{timestamp}.xlsx',
        as_attachment=True
    )

@app.route('/admin/qr')
@admin_required
def generate_qr():
    host_url = request.host_url
    target_url = host_url.rstrip('/') + url_for('student_portal')
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(target_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    img_io = BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    
    return send_file(img_io, mimetype='image/png')
    
@app.route('/admin/live_stats')
@admin_required
def live_stats():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM attendees")
        total_registered = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM attendees WHERE status = 'attended'")
        total_attended = cursor.fetchone()[0]
        
        cursor.execute("SELECT name, phone, check_in_time FROM attendees WHERE status = 'attended' ORDER BY check_in_time DESC LIMIT 10")
        recent = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        
    return jsonify({
        'total_registered': total_registered,
        'total_attended': total_attended,
        'recent': recent
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
