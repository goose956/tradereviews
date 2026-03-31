import sqlite3
conn = sqlite3.connect("local.db")
c = conn.cursor()
c.execute("DELETE FROM line_items WHERE parent_id IN (SELECT id FROM invoices WHERE status = 'draft')")
print(f"Cleaned {c.rowcount} line items")
c.execute("DELETE FROM invoices WHERE status = 'draft'")
print(f"Cleaned {c.rowcount} draft invoices")
conn.commit()
conn.close()
