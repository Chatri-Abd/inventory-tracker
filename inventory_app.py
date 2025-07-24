from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, flash
import sqlite3
import qrcode
import io
import base64
import os
import csv
from datetime import datetime
import uuid
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Database setup
def init_db():
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    # Items table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            category TEXT,
            location TEXT,
            quantity INTEGER DEFAULT 1,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            qr_code TEXT
        )
    ''')
    
    # Transactions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT,
            action TEXT,
            quantity INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            location TEXT,
            notes TEXT,
            FOREIGN KEY (item_id) REFERENCES items (id)
        )
    ''')
    
    conn.commit()
    conn.close()

def generate_qr_code(item_id):
    """Generate QR code for item ID"""
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(item_id)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert to base64 string
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()
    
    return f"data:image/png;base64,{img_str}"

@app.route('/')
def index():
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    # Get all items
    cursor.execute('''
        SELECT id, name, description, category, location, quantity, date_added
        FROM items ORDER BY name
    ''')
    items = cursor.fetchall()
    
    # Get unique locations and categories for filters
    cursor.execute('SELECT DISTINCT location FROM items WHERE location IS NOT NULL')
    locations = [row[0] for row in cursor.fetchall()]
    
    cursor.execute('SELECT DISTINCT category FROM items WHERE category IS NOT NULL')
    categories = [row[0] for row in cursor.fetchall()]
    
    conn.close()
    
    return render_template('index.html', items=items, locations=locations, categories=categories)

@app.route('/add_item', methods=['GET', 'POST'])
def add_item():
    if request.method == 'POST':
        item_id = str(uuid.uuid4())[:8]  # Short unique ID
        name = request.form['name']
        description = request.form.get('description', '')
        category = request.form.get('category', '')
        location = request.form.get('location', '')
        quantity = int(request.form.get('quantity', 1))
        
        # Generate QR code
        qr_code = generate_qr_code(item_id)
        
        conn = sqlite3.connect('inventory.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO items (id, name, description, category, location, quantity, qr_code)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (item_id, name, description, category, location, quantity, qr_code))
        
        # Log transaction
        cursor.execute('''
            INSERT INTO transactions (item_id, action, quantity, location, notes)
            VALUES (?, 'added', ?, ?, 'Item added to inventory')
        ''', (item_id, quantity, location))
        
        conn.commit()
        conn.close()
        
        flash(f'Item "{name}" added successfully with ID: {item_id}')
        return redirect(url_for('index'))
    
    return render_template('add_item.html')

@app.route('/item/<item_id>')
def item_detail(item_id):
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    # Get item details
    cursor.execute('SELECT * FROM items WHERE id = ?', (item_id,))
    item = cursor.fetchone()
    
    if not item:
        flash('Item not found')
        return redirect(url_for('index'))
    
    # Get transaction history
    cursor.execute('''
        SELECT action, quantity, timestamp, location, notes
        FROM transactions WHERE item_id = ?
        ORDER BY timestamp DESC
    ''', (item_id,))
    transactions = cursor.fetchall()
    
    conn.close()
    
    return render_template('item_detail.html', item=item, transactions=transactions)

@app.route('/check_in_out/<item_id>', methods=['POST'])
def check_in_out(item_id):
    action = request.form['action']  # 'check_in' or 'check_out'
    quantity = int(request.form.get('quantity', 1))
    location = request.form.get('location', '')
    notes = request.form.get('notes', '')
    
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    # Get current item
    cursor.execute('SELECT quantity, location FROM items WHERE id = ?', (item_id,))
    current = cursor.fetchone()
    
    if not current:
        return jsonify({'error': 'Item not found'}), 404
    
    current_qty, current_location = current
    
    # Update quantity and location
    if action == 'check_in':
        new_qty = current_qty + quantity
    else:  # check_out
        new_qty = max(0, current_qty - quantity)
    
    new_location = location if location else current_location
    
    cursor.execute('''
        UPDATE items SET quantity = ?, location = ?, last_updated = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (new_qty, new_location, item_id))
    
    # Log transaction
    cursor.execute('''
        INSERT INTO transactions (item_id, action, quantity, location, notes)
        VALUES (?, ?, ?, ?, ?)
    ''', (item_id, action, quantity, new_location, notes))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'new_quantity': new_qty})

@app.route('/bulk_upload', methods=['GET', 'POST'])
def bulk_upload():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file selected')
            return redirect(request.url)
        
        file = request.files['file']
        if file.filename == '':
            flash('No file selected')
            return redirect(request.url)
        
        if file and file.filename.lower().endswith('.csv'):
            try:
                # Read CSV file
                csv_content = file.read().decode('utf-8')
                csv_reader = csv.DictReader(io.StringIO(csv_content))
                
                # Validate required columns
                required_columns = ['name']
                if not all(col in csv_reader.fieldnames for col in required_columns):
                    flash('CSV must have at least a "name" column. Optional columns: description, category, location, quantity')
                    return redirect(request.url)
                
                conn = sqlite3.connect('inventory.db')
                cursor = conn.cursor()
                
                items_added = 0
                errors = []
                
                for row_num, row in enumerate(csv_reader, start=2):  # Start at 2 (header is row 1)
                    try:
                        # Required field
                        name = row.get('name', '').strip()
                        if not name:
                            errors.append(f"Row {row_num}: Name is required")
                            continue
                        
                        # Optional fields
                        description = row.get('description', '').strip()
                        category = row.get('category', '').strip()
                        location = row.get('location', '').strip()
                        
                        # Handle quantity
                        quantity_str = row.get('quantity', '1').strip()
                        try:
                            quantity = int(quantity_str) if quantity_str else 1
                            if quantity < 1:
                                quantity = 1
                        except ValueError:
                            quantity = 1
                        
                        # Generate unique ID and QR code
                        item_id = str(uuid.uuid4())[:8]
                        qr_code = generate_qr_code(item_id)
                        
                        # Insert item
                        cursor.execute('''
                            INSERT INTO items (id, name, description, category, location, quantity, qr_code)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (item_id, name, description, category, location, quantity, qr_code))
                        
                        # Log transaction
                        cursor.execute('''
                            INSERT INTO transactions (item_id, action, quantity, location, notes)
                            VALUES (?, 'added', ?, ?, 'Bulk upload from CSV')
                        ''', (item_id, quantity, location))
                        
                        items_added += 1
                        
                    except Exception as e:
                        errors.append(f"Row {row_num}: {str(e)}")
                
                conn.commit()
                conn.close()
                
                # Show results
                if items_added > 0:
                    flash(f'Successfully added {items_added} items!')
                
                if errors:
                    flash(f'Errors encountered: {"; ".join(errors[:5])}{"..." if len(errors) > 5 else ""}')
                
                return redirect(url_for('index'))
                
            except Exception as e:
                flash(f'Error processing CSV file: {str(e)}')
                return redirect(request.url)
        else:
            flash('Please upload a CSV file')
            return redirect(request.url)
    
    return render_template('bulk_upload.html')

@app.route('/download_template')
def download_template():
    """Download CSV template file"""
    template_data = [
        ['name', 'description', 'category', 'location', 'quantity'],
        ['Screwdriver Set', 'Phillips and flathead screwdrivers', 'Tools', 'Garage', '1'],
        ['Laptop Charger', 'Dell 65W USB-C charger', 'Electronics', 'Office', '2'],
        ['First Aid Kit', 'Emergency medical supplies', 'Safety', 'Car', '1'],
        ['Bluetooth Speaker', 'Portable wireless speaker', 'Electronics', 'Home', '1']
    ]
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(template_data)
    
    response = app.response_class(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=inventory_template.csv'}
    )
    return response

@app.route('/export_inventory')
def export_inventory():
    """Export current inventory to CSV"""
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, name, description, category, location, quantity, date_added, last_updated
        FROM items ORDER BY name
    ''')
    items = cursor.fetchall()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow(['id', 'name', 'description', 'category', 'location', 'quantity', 'date_added', 'last_updated'])
    
    # Write data
    writer.writerows(items)
    
    response = app.response_class(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=inventory_export.csv'}
    )
    return response

@app.route('/scan')
def scan():
    return render_template('scan.html')
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM items WHERE id = ?', (item_id,))
    item = cursor.fetchone()
    
    conn.close()
    
    if not item:
        return jsonify({'error': 'Item not found'}), 404
    
    return jsonify({
        'id': item[0],
        'name': item[1],
        'description': item[2],
        'category': item[3],
        'location': item[4],
        'quantity': item[5],
        'date_added': item[6],
        'qr_code': item[8]
    })

@app.route('/search')
def search():
    query = request.args.get('q', '')
    location = request.args.get('location', '')
    category = request.args.get('category', '')
    
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    sql = 'SELECT * FROM items WHERE 1=1'
    params = []
    
    if query:
        sql += ' AND (name LIKE ? OR description LIKE ?)'
        params.extend([f'%{query}%', f'%{query}%'])
    
    if location:
        sql += ' AND location = ?'
        params.append(location)
    
    if category:
        sql += ' AND category = ?'
        params.append(category)
    
    cursor.execute(sql, params)
    items = cursor.fetchall()
    
    conn.close()
    
    return jsonify([{
        'id': item[0],
        'name': item[1],
        'description': item[2],
        'category': item[3],
        'location': item[4],
        'quantity': item[5]
    } for item in items])

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)