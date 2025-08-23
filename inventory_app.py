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
    
    # Counter table for sequential IDs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS id_counter (
            counter INTEGER DEFAULT 0
        )
    ''')
    
    # Initialize counter if empty
    cursor.execute('SELECT COUNT(*) FROM id_counter')
    if cursor.fetchone()[0] == 0:
        cursor.execute('INSERT INTO id_counter (counter) VALUES (0)')
    
    conn.commit()
    conn.close()

def generate_item_id():
    """Generate sequential ID in format P0000001, P0000002, etc."""
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    # Get and increment counter
    cursor.execute('SELECT counter FROM id_counter')
    current_counter = cursor.fetchone()[0]
    new_counter = current_counter + 1
    
    cursor.execute('UPDATE id_counter SET counter = ?', (new_counter,))
    conn.commit()
    conn.close()
    
    # Format as P + 7 digits with leading zeros
    return f"P{new_counter:07d}"
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
        item_id = generate_item_id()  # Generate sequential ID
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
                        item_id = generate_item_id()  # Use sequential ID generator
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
# ==============================================
# 1. FLASK BACKEND UPDATES 
# ==============================================

#  route for DELETE functionality
@app.route('/delete_item/<item_id>', methods=['POST'])
def delete_item(item_id):
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    # Check if item exists
    cursor.execute('SELECT name FROM items WHERE id = ?', (item_id,))
    item = cursor.fetchone()
    
    if not item:
        flash('Item not found')
        return redirect(url_for('index'))
    
    try:
        # Delete from transactions first (foreign key constraint)
        cursor.execute('DELETE FROM transactions WHERE item_id = ?', (item_id,))
        
        # Delete the item
        cursor.execute('DELETE FROM items WHERE id = ?', (item_id,))
        
        conn.commit()
        flash(f'Item "{item[0]}" deleted successfully!')
        
    except Exception as e:
        conn.rollback()
        flash(f'Error deleting item: {str(e)}')
    
    finally:
        conn.close()
    
    return redirect(url_for('index'))

# route for SEARCH functionality (fix the existing search)
@app.route('/api/search')
def api_search():
    query = request.args.get('q', '').strip()
    
    if not query:
        return jsonify({'error': 'Query parameter required'}), 400
    
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    # Search by exact ID or partial name/description match
    cursor.execute('''
        SELECT id, name, description, category, location, quantity, qr_code
        FROM items 
        WHERE id = ? OR name LIKE ? OR description LIKE ?
        ORDER BY 
            CASE WHEN id = ? THEN 0 ELSE 1 END,
            name
    ''', (query, f'%{query}%', f'%{query}%', query))
    
    items = cursor.fetchall()
    conn.close()
    
    results = []
    for item in items:
        results.append({
            'id': item[0],
            'name': item[1],
            'description': item[2] or '',
            'category': item[3] or '',
            'location': item[4] or '',
            'quantity': item[5],
            'qr_code': item[6] or ''
        })
    
    return jsonify(results)

# route for PRINT functionality
@app.route('/print/<print_type>/<item_id>')
def print_item(print_type, item_id):
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM items WHERE id = ?', (item_id,))
    item = cursor.fetchone()
    conn.close()
    
    if not item:
        flash('Item not found')
        return redirect(url_for('index'))
    
    if print_type == 'qr_only':
        return render_template('print_qr_only.html', item=item)
    elif print_type == 'qr_with_info':
        return render_template('print_qr_with_info.html', item=item)
    else:
        flash('Invalid print type')
        return redirect(url_for('item_detail', item_id=item_id))

# ==============================================
# DATABASE BACKUP SOLUTION
# ==============================================

import shutil
import zipfile
from datetime import datetime
import os

@app.route('/backup_database')
def backup_database():
    """Create and download database backup"""
    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f'inventory_backup_{timestamp}.zip'
        
        # Create temporary backup directory
        backup_dir = 'temp_backup'
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        
        # Copy database file
        if os.path.exists('inventory.db'):
            shutil.copy2('inventory.db', os.path.join(backup_dir, 'inventory.db'))
        
        # Export data to CSV as well
        conn = sqlite3.connect('inventory.db')
        cursor = conn.cursor()
        
        # Export items
        cursor.execute('SELECT * FROM items ORDER BY date_added')
        items = cursor.fetchall()
        
        # Export transactions
        cursor.execute('SELECT * FROM transactions ORDER BY timestamp')
        transactions = cursor.fetchall()
        
        conn.close()
        
        # Write items CSV
        with open(os.path.join(backup_dir, 'items_backup.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'name', 'description', 'category', 'location', 'quantity', 'date_added', 'last_updated', 'qr_code'])
            writer.writerows(items)
        
        # Write transactions CSV
        with open(os.path.join(backup_dir, 'transactions_backup.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'item_id', 'action', 'quantity', 'timestamp', 'location', 'notes'])
            writer.writerows(transactions)
        
        # Create README file
        readme_content = f"""INVENTORY DATABASE BACKUP
Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

FILES INCLUDED:
- inventory.db: SQLite database file (can be used to restore full database)
- items_backup.csv: All inventory items in CSV format
- transactions_backup.csv: All transaction history in CSV format

RESTORE INSTRUCTIONS:
1. Replace your current inventory.db with the backed up inventory.db file
2. Or use the CSV files to import data into a new database

BACKUP STATISTICS:
- Total Items: {len(items)}
- Total Transactions: {len(transactions)}
"""
        
        with open(os.path.join(backup_dir, 'README.txt'), 'w') as f:
            f.write(readme_content)
        
        # Create ZIP file
        zip_path = os.path.join('temp', backup_filename)
        if not os.path.exists('temp'):
            os.makedirs('temp')
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(backup_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, backup_dir)
                    zipf.write(file_path, arcname)
        
        # Clean up temporary directory
        shutil.rmtree(backup_dir)
        
        # Send file
        def remove_file(response):
            try:
                os.remove(zip_path)
            except Exception:
                pass
            return response
        
        return send_from_directory('temp', backup_filename, as_attachment=True)
        
    except Exception as e:
        flash(f'Error creating backup: {str(e)}')
        return redirect(url_for('index'))

@app.route('/restore_database', methods=['GET', 'POST'])
def restore_database():
    """Restore database from backup file"""
    if request.method == 'POST':
        if 'backup_file' not in request.files:
            flash('No file selected')
            return redirect(request.url)
        
        file = request.files['backup_file']
        if file.filename == '':
            flash('No file selected')
            return redirect(request.url)
        
        if file and file.filename.lower().endswith('.zip'):
            try:
                # Create temporary directory for extraction
                extract_dir = 'temp_restore'
                if os.path.exists(extract_dir):
                    shutil.rmtree(extract_dir)
                os.makedirs(extract_dir)
                
                # Save uploaded file
                zip_path = os.path.join(extract_dir, 'backup.zip')
                file.save(zip_path)
                
                # Extract ZIP file
                with zipfile.ZipFile(zip_path, 'r') as zipf:
                    zipf.extractall(extract_dir)
                
                # Check if database file exists in backup
                db_backup_path = os.path.join(extract_dir, 'inventory.db')
                if os.path.exists(db_backup_path):
                    # Backup current database
                    if os.path.exists('inventory.db'):
                        backup_current = f'inventory_backup_before_restore_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
                        shutil.copy2('inventory.db', backup_current)
                        flash(f'Current database backed up as: {backup_current}')
                    
                    # Restore database
                    shutil.copy2(db_backup_path, 'inventory.db')
                    flash('Database restored successfully!')
                else:
                    flash('No database file found in backup')
                
                # Clean up
                shutil.rmtree(extract_dir)
                
                return redirect(url_for('index'))
                
            except Exception as e:
                flash(f'Error restoring database: {str(e)}')
                if os.path.exists('temp_restore'):
                    shutil.rmtree('temp_restore')
                return redirect(request.url)
        else:
            flash('Please upload a ZIP file')
            return redirect(request.url)
    
    return render_template('restore_database.html')

@app.route('/database_info')
def database_info():
    """Show database statistics and information"""
    conn = sqlite3.connect('inventory.db')
    cursor = conn.cursor()
    
    # Get database statistics
    cursor.execute('SELECT COUNT(*) FROM items')
    total_items = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM transactions')
    total_transactions = cursor.fetchone()[0]
    
    cursor.execute('SELECT SUM(quantity) FROM items')
    total_quantity = cursor.fetchone()[0] or 0
    
    cursor.execute('SELECT COUNT(DISTINCT location) FROM items WHERE location IS NOT NULL')
    unique_locations = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(DISTINCT category) FROM items WHERE category IS NOT NULL')
    unique_categories = cursor.fetchone()[0]
    
    # Get database file size
    db_size = os.path.getsize('inventory.db') if os.path.exists('inventory.db') else 0
    db_size_mb = round(db_size / (1024 * 1024), 2)
    
    # Get recent activity
    cursor.execute('''
        SELECT i.name, t.action, t.quantity, t.timestamp 
        FROM transactions t 
        JOIN items i ON t.item_id = i.id 
        ORDER BY t.timestamp DESC 
        LIMIT 10
    ''')
    recent_activity = cursor.fetchall()
    
    conn.close()
    
    stats = {
        'total_items': total_items,
        'total_transactions': total_transactions,
        'total_quantity': total_quantity,
        'unique_locations': unique_locations,
        'unique_categories': unique_categories,
        'db_size_mb': db_size_mb,
        'recent_activity': recent_activity
    }
    
    return render_template('database_info.html', stats=stats)

# ==============================================
# SCHEDULED BACKUP (Optional - for automatic backups)
# ==============================================

def create_automated_backup():
    """Create automated backup - can be called by a scheduler"""
    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = 'automated_backups'
        
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        
        # Keep only last 30 backups
        existing_backups = [f for f in os.listdir(backup_dir) if f.startswith('auto_backup_')]
        existing_backups.sort(reverse=True)
        
        # Remove old backups (keep last 30)
        for old_backup in existing_backups[30:]:
            try:
                os.remove(os.path.join(backup_dir, old_backup))
            except:
                pass
        
        # Create new backup
        backup_filename = f'auto_backup_{timestamp}.db'
        shutil.copy2('inventory.db', os.path.join(backup_dir, backup_filename))
        
        return True
    except Exception as e:
        print(f"Automated backup failed: {e}")
        return False

# ==============================================
# DATABASE STORAGE RECOMMENDATIONS
# ==============================================

"""
CURRENT SETUP (SQLite) - PERFECT FOR YOUR USE CASE:

✅ ADVANTAGES:
- Single file storage (inventory.db)
- No server setup required
- Fast for personal use
- Easy backup (just copy the .db file)
- Supports up to 281TB database size
- ACID compliant (reliable)
- Works offline

📁 BACKUP STRATEGY:
1. Manual backups via /backup_database route
2. File system backups (copy inventory.db)
3. CSV exports for data portability
4. Automated daily backups (optional)

🔄 MIGRATION OPTIONS (if needed later):
- SQLite → MySQL: Use sqlite3 and mysql connectors
- SQLite → PostgreSQL: Use pgloader or custom scripts
- SQLite → Cloud: Export to CSV then import

💾 STORAGE LOCATIONS:
- Local: Current setup (inventory.db file)
- Cloud backup: Upload .db file to Google Drive/Dropbox
- Version control: Git repository with database
- Network: Shared folder for multi-device access

🚀 SCALING OPTIONS:
- Current: SQLite (perfect for 1-100,000 items)
- Medium scale: MySQL/PostgreSQL (100,000+ items)
- Large scale: Cloud databases (millions of items)

RECOMMENDATION: 
Keep SQLite! It's perfect for personal inventory tracking.
Add regular backups using the backup routes above.
"""
