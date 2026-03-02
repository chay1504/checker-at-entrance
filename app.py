import os
import sqlite3
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, flash
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
        try:
            conn.execute('CREATE INDEX idx_unique_code ON attendees(unique_code)')
        except sqlite3.OperationalError:
            pass # Index already exists
        conn.commit()
        conn.close()

init_db()

@app.route('/', methods=['GET'])
def student_portal():
    return render_template('index.html')

@app.route('/checkin', methods=['POST'])
def checkin_api():
    code = request.form.get('unique_code', '').strip()
    if not code:
        return jsonify({'success': False, 'message': 'Code is required.', 'type': 'error'})
    
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM attendees WHERE unique_code = ?", (code,))
            attendee = cursor.fetchone()
            
            if not attendee:
                conn.close()
                return jsonify({'success': False, 'message': 'Invalid Code. Please try again.', 'type': 'error'})
            
            status = attendee['status']
            name = attendee['name']
            
            if status == 'attended':
                conn.close()
                return jsonify({'success': False, 'message': 'Already Checked In', 'type': 'warning'})
            
            # Update to attended
            current_time = datetime.now()
            cursor.execute(
                "UPDATE attendees SET status = 'attended', check_in_time = ? WHERE unique_code = ?", 
                (current_time, code)
            )
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': f'Welcome, {name}!', 'type': 'success'})
    except Exception as e:
        return jsonify({'success': False, 'message': 'Database Error. Please try again.', 'type': 'error'})

@app.route('/admin/import', methods=['GET', 'POST'])
def admin_import():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part', 'error')
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
                # Expected Columns: Name, Phone, College, Unique_Code
                required_cols = {'Name', 'Phone', 'College', 'Unique_Code'}
                if not required_cols.issubset(set(df.columns)):
                    flash('Invalid Excel format. Make sure columns are: Name, Phone, College, Unique_Code', 'error')
                    os.remove(filepath)
                    return redirect(request.url)
                    
                records_added = 0
                records_failed = 0
                
                with db_lock:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    for _, row in df.iterrows():
                        try:
                            # Avoid duplicates by ignoring existing codes
                            cursor.execute("SELECT id FROM attendees WHERE unique_code = ?", (str(row['Unique_Code']),))
                            if not cursor.fetchone():
                                cursor.execute('''
                                    INSERT INTO attendees (name, phone, college, unique_code, status)
                                    VALUES (?, ?, ?, ?, 'unattended')
                                ''', (
                                    str(row['Name']),
                                    str(row.get('Phone', '')),
                                    str(row.get('College', '')),
                                    str(row['Unique_Code'])
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
            flash('Please upload a valid .xlsx file', 'error')
            
    return render_template('admin_import.html')

@app.route('/admin/dashboard')
def admin_dashboard():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM attendees")
        total_registered = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM attendees WHERE status = 'attended'")
        total_attended = cursor.fetchone()[0]
        
        conn.close()
        
    return render_template('admin_dashboard.html', 
                           total_registered=total_registered, 
                           total_attended=total_attended)

@app.route('/admin/export')
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
def generate_qr():
    host_url = request.host_url
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(host_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    img_io = BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    
    return send_file(img_io, mimetype='image/png')
    
@app.route('/admin/live_stats')
def live_stats():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM attendees")
        total_registered = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM attendees WHERE status = 'attended'")
        total_attended = cursor.fetchone()[0]
        
        conn.close()
        
    return jsonify({
        'total_registered': total_registered,
        'total_attended': total_attended
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
