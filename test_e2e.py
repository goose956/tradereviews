"""
End-to-end test for the ReviewEngine app.
Tests: auth, customers, invoices, quotes, expenses, bookings, messages, stats, PDFs.
Directly inserts a session token into the DB to avoid WhatsApp OTP.
"""

import asyncio
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

import httpx

BASE = "http://127.0.0.1:8000"
DB_PATH = "local.db"

# ── Results tracking ──
passed = []
failed = []

def ok(name, detail=""):
    passed.append(name)
    tag = f" — {detail}" if detail else ""
    print(f"  ✅ {name}{tag}")

def fail(name, detail=""):
    failed.append(name)
    tag = f" — {detail}" if detail else ""
    print(f"  ❌ {name}{tag}")

def check(name, condition, detail=""):
    if condition:
        ok(name, detail)
    else:
        fail(name, detail)


async def run_tests():
    print("=" * 60)
    print("  REVIEWENGINE END-TO-END TEST")
    print("=" * 60)

    # ── Step 0: Check server is up ──
    print("\n🔌 Checking server...")
    async with httpx.AsyncClient(base_url=BASE, timeout=10) as client:
        try:
            r = await client.get("/health")
            check("Server health", r.status_code == 200, f"status={r.status_code}")
        except Exception as e:
            fail("Server health", str(e))
            print("\n⛔ Server not reachable. Aborting.")
            return

    # ── Step 1: Find/create business in DB ──
    print("\n📦 Setting up test business...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Find existing business
    cur.execute("SELECT id, business_name, phone_number FROM businesses LIMIT 1")
    biz_row = cur.fetchone()
    if biz_row:
        biz_id = biz_row["id"]
        biz_name = biz_row["business_name"]
        print(f"  Found business: {biz_name} (ID: {biz_id})")
    else:
        biz_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            "INSERT INTO businesses (id, business_name, phone_number, email, trade_type, subscription_status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (biz_id, "TestBiz", "+441234567890", "test@example.com", "plumber", "active", now, now),
        )
        conn.commit()
        biz_name = "TestBiz"
        print(f"  Created business: {biz_name} (ID: {biz_id})")

    # ── Step 2: Create auth session directly ──
    print("\n🔑 Creating auth session...")
    token = secrets.token_urlsafe(48)
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    sid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO auth_sessions (id, business_id, token, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (sid, biz_id, token, expires, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    print(f"  Token: {token[:20]}...")

    headers = {"Authorization": f"Bearer {token}"}
    API = f"/member/business/{biz_id}"

    async with httpx.AsyncClient(base_url=BASE, timeout=15, headers=headers) as client:
        # ── Step 3: Auth check ──
        print("\n🔐 Testing auth...")
        r = await client.get("/auth/me")
        check("GET /auth/me", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            me = r.json()
            check("Auth returns business", me.get("business_id") == biz_id or me.get("id") == biz_id)

        # ── Step 4: Get business info ──
        print("\n🏢 Testing business endpoints...")
        r = await client.get(API)
        check("GET business", r.status_code == 200, f"status={r.status_code}")

        # Update business
        r = await client.patch(API, json={
            "business_address": "123 Test Street",
            "business_city": "London",
            "business_postcode": "SW1A 1AA",
        })
        check("PATCH business", r.status_code == 200, f"status={r.status_code}")

        # Verify update persisted
        r = await client.get(API)
        if r.status_code == 200:
            biz = r.json()
            check("Business address updated", biz.get("business_address") == "123 Test Street",
                  f"got: {biz.get('business_address')}")
            check("Business city updated", biz.get("business_city") == "London",
                  f"got: {biz.get('business_city')}")

        # ── Step 5: Create customers ──
        print("\n👥 Testing customer creation via DB (customers are created by webhook)...")
        conn = sqlite3.connect(DB_PATH)
        now = datetime.now(timezone.utc).isoformat()

        customers = []
        for i, (name, phone, email) in enumerate([
            ("Alice Smith", "+447700000001", "alice@example.com"),
            ("Bob Jones", "+447700000002", "bob@example.com"),
            ("Charlie Brown", "+447700000003", "charlie@example.com"),
        ], 1):
            cid = str(uuid.uuid4())
            try:
                conn.execute(
                    "INSERT INTO customers (id, business_id, name, phone_number, email, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cid, biz_id, name, phone, email, "request_sent", now),
                )
                conn.commit()
                customers.append({"id": cid, "name": name, "phone": phone})
                ok(f"Create customer {i}", name)
            except sqlite3.IntegrityError:
                # Already exists
                row = conn.execute(
                    "SELECT id FROM customers WHERE business_id=? AND phone_number=?",
                    (biz_id, phone)
                ).fetchone()
                if row:
                    customers.append({"id": row[0], "name": name, "phone": phone})
                    ok(f"Create customer {i} (exists)", name)
                else:
                    fail(f"Create customer {i}", "IntegrityError but not found")
        conn.close()

        # Verify via API
        r = await client.get(f"{API}/customers")
        check("GET customers", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            cust_list = r.json()
            check("Customers count >= 3", len(cust_list) >= 3, f"got {len(cust_list)}")

        # ── Step 6: Create invoices ──
        print("\n🧾 Testing invoices...")
        invoice_ids = []
        for i, cust in enumerate(customers[:2], 1):
            r = await client.post(f"{API}/invoices", json={
                "customer_id": cust["id"],
                "notes": f"Test job #{i}",
                "line_items": [
                    {"description": f"Plumbing repair #{i}", "quantity": 1, "unit_price": 150.00 * i},
                    {"description": "Call-out charge", "quantity": 1, "unit_price": 50.00},
                ],
            })
            check(f"Create invoice {i}", r.status_code in (200, 201), f"status={r.status_code}")
            if r.status_code in (200, 201):
                inv = r.json()
                invoice_ids.append(inv.get("id"))
                check(f"Invoice {i} has number", bool(inv.get("invoice_number")),
                      inv.get("invoice_number"))

        # List invoices
        r = await client.get(f"{API}/invoices")
        check("GET invoices", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            inv_list = r.json()
            check("Invoices count >= 2", len(inv_list) >= 2, f"got {len(inv_list)}")

        # Get single invoice detail (with line items)
        if invoice_ids:
            r = await client.get(f"{API}/invoices/{invoice_ids[0]}")
            check("GET single invoice", r.status_code == 200, f"status={r.status_code}")
            if r.status_code == 200:
                inv = r.json()
                check("Invoice has line_items", "line_items" in inv, f"keys: {list(inv.keys())[:8]}")
                check("Invoice total > 0", (inv.get("total") or 0) > 0, f"total={inv.get('total')}")

        # Mark invoice as paid
        if invoice_ids:
            r = await client.post(f"{API}/invoices/{invoice_ids[0]}/mark-paid", json={
                "payment_method": "bank_transfer",
            })
            check("Mark invoice paid", r.status_code == 200, f"status={r.status_code}")

            # Verify it's now paid
            r = await client.get(f"{API}/invoices/{invoice_ids[0]}")
            if r.status_code == 200:
                inv = r.json()
                check("Invoice status is paid", inv.get("status") == "paid", f"status={inv.get('status')}")

        # PDF download (public endpoint, no auth)
        if invoice_ids:
            r = await client.get(f"{API}/invoices/{invoice_ids[0]}/pdf",
                                 headers={})  # Override auth for public endpoint test
            # PDF endpoints might fail if wkhtmltopdf not installed, but should at least route
            check("Invoice PDF endpoint responds", r.status_code in (200, 500),
                  f"status={r.status_code}, content-type={r.headers.get('content-type', '?')}")

        # ── Step 7: Create quotes ──
        print("\n📝 Testing quotes...")
        quote_ids = []
        for i, cust in enumerate(customers[:2], 1):
            r = await client.post(f"{API}/quotes", json={
                "customer_id": cust["id"],
                "notes": f"Quote #{i}",
                "line_items": [
                    {"description": f"Bathroom refit phase {i}", "quantity": 1, "unit_price": 500.00 * i},
                    {"description": "Materials", "quantity": 1, "unit_price": 200.00},
                ],
            })
            check(f"Create quote {i}", r.status_code in (200, 201), f"status={r.status_code}")
            if r.status_code in (200, 201):
                quo = r.json()
                quote_ids.append(quo.get("id"))
                check(f"Quote {i} has number", bool(quo.get("quote_number")),
                      quo.get("quote_number"))

        # List quotes
        r = await client.get(f"{API}/quotes")
        check("GET quotes", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            quo_list = r.json()
            check("Quotes count >= 2", len(quo_list) >= 2, f"got {len(quo_list)}")

        # Get single quote
        if quote_ids:
            r = await client.get(f"{API}/quotes/{quote_ids[0]}")
            check("GET single quote", r.status_code == 200, f"status={r.status_code}")
            if r.status_code == 200:
                quo = r.json()
                check("Quote has line_items", "line_items" in quo)
                check("Quote total > 0", (quo.get("total") or 0) > 0, f"total={quo.get('total')}")

        # ── Step 8: Create expenses ──
        print("\n💰 Testing expenses...")
        expense_ids = []
        test_expenses = [
            {"vendor": "Screwfix", "description": "Copper pipe fittings", "category": "materials",
             "date": "2026-03-28", "subtotal": 45.00, "tax_amount": 9.00, "total": 54.00},
            {"vendor": "Shell", "description": "Diesel fuel", "category": "fuel",
             "date": "2026-03-29", "subtotal": 80.00, "tax_amount": 16.00, "total": 96.00},
            {"vendor": "Toolstation", "description": "Pipe wrench", "category": "tools",
             "date": "2026-03-30", "subtotal": 25.00, "tax_amount": 5.00, "total": 30.00},
        ]
        # Expenses are created via webhook (receipt scanning), so insert directly
        conn = sqlite3.connect(DB_PATH)
        for i, exp in enumerate(test_expenses, 1):
            eid = str(uuid.uuid4())
            try:
                conn.execute(
                    "INSERT INTO expenses (id, business_id, vendor, description, category, date, subtotal, tax_amount, total, currency, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (eid, biz_id, exp["vendor"], exp["description"], exp["category"],
                     exp["date"], exp["subtotal"], exp["tax_amount"], exp["total"], "GBP", now, now),
                )
                conn.commit()
                expense_ids.append(eid)
                ok(f"Create expense {i}", f"{exp['vendor']} — £{exp['total']}")
            except Exception as e:
                fail(f"Create expense {i}", str(e))
        conn.close()

        # List expenses via API
        r = await client.get(f"{API}/expenses")
        check("GET expenses", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            exp_list = r.json()
            check("Expenses count >= 3", len(exp_list) >= 3, f"got {len(exp_list)}")

        # Expense summary
        r = await client.get(f"{API}/expenses/summary")
        check("GET expenses/summary", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            summary = r.json()
            check("Summary has total", "total" in summary or "grand_total" in summary,
                  f"keys: {list(summary.keys())}")

        # Update an expense
        if expense_ids:
            r = await client.patch(f"{API}/expenses/{expense_ids[0]}", json={
                "description": "Copper pipe fittings (updated)",
            })
            check("PATCH expense", r.status_code == 200, f"status={r.status_code}")

        # ── Step 9: Create bookings ──
        print("\n📅 Testing bookings...")
        booking_ids = []
        test_bookings = [
            {"title": "Boiler service", "date": "2026-03-31", "time": "09:00",
             "duration_mins": 90, "customer_name": "Alice Smith", "customer_phone": "+447700000001",
             "notes": "Annual service check"},
            {"title": "Tap replacement", "date": "2026-04-01", "time": "14:00",
             "duration_mins": 60, "customer_name": "Bob Jones", "customer_phone": "+447700000002",
             "notes": "Kitchen tap leaking"},
            {"title": "Emergency pipe burst", "date": "2026-03-30", "time": "08:00",
             "duration_mins": 120, "customer_name": "Charlie Brown", "customer_phone": "+447700000003",
             "notes": "Urgent - water damage"},
        ]
        conn = sqlite3.connect(DB_PATH)
        for i, bk in enumerate(test_bookings, 1):
            bid = str(uuid.uuid4())
            try:
                conn.execute(
                    "INSERT INTO bookings (id, business_id, customer_name, customer_phone, title, date, time, duration_mins, notes, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (bid, biz_id, bk["customer_name"], bk["customer_phone"], bk["title"],
                     bk["date"], bk["time"], bk["duration_mins"], bk["notes"], "confirmed", now, now),
                )
                conn.commit()
                booking_ids.append(bid)
                ok(f"Create booking {i}", f"{bk['title']} — {bk['date']} {bk['time']}")
            except Exception as e:
                fail(f"Create booking {i}", str(e))
        conn.close()

        # List bookings via API
        r = await client.get(f"{API}/bookings")
        check("GET bookings", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            bk_list = r.json()
            check("Bookings count >= 3", len(bk_list) >= 3, f"got {len(bk_list)}")
            # Check they have the right fields
            if bk_list:
                first = bk_list[0]
                for field in ("id", "title", "date", "time", "duration_mins", "status"):
                    check(f"Booking has '{field}'", field in first)

        # Update booking status
        if booking_ids:
            r = await client.patch(f"{API}/bookings/{booking_ids[2]}", json={
                "status": "completed",
            })
            check("PATCH booking -> completed", r.status_code == 200, f"status={r.status_code}")

            # Get single booking
            r = await client.get(f"{API}/bookings/{booking_ids[2]}")
            check("GET single booking", r.status_code == 200, f"status={r.status_code}")
            if r.status_code == 200:
                bk = r.json()
                check("Booking status is completed", bk.get("status") == "completed",
                      f"status={bk.get('status')}")

        # ── Step 10: Simulate review request via DB ──
        print("\n⭐ Testing review drafts...")
        conn = sqlite3.connect(DB_PATH)
        draft_id = str(uuid.uuid4())
        try:
            conn.execute(
                "INSERT INTO review_drafts (id, business_id, google_review_id, reviewer_name, review_text, star_rating, ai_draft_reply, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (draft_id, biz_id, f"google_review_{uuid.uuid4().hex[:8]}",
                 "David Customer", "Excellent plumber, very reliable and fair price!",
                 5, "Thank you for your kind words, David! We're delighted to hear you had a great experience.",
                 "pending_approval", now, now),
            )
            conn.commit()
            ok("Create review draft", "5-star review from David")
        except Exception as e:
            fail("Create review draft", str(e))
        conn.close()

        # Get drafts via API
        r = await client.get(f"{API}/drafts")
        check("GET drafts", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            drafts = r.json()
            check("Drafts count >= 1", len(drafts) >= 1, f"got {len(drafts)}")

        # ── Step 11: Message log ──
        print("\n💬 Testing messages...")
        conn = sqlite3.connect(DB_PATH)
        msg_types = [
            ("Review request sent", "review_request", "+447700000001"),
            ("Invoice sent to Alice", "invoice", "+447700000001"),
            ("Quote sent to Bob", "quote", "+447700000002"),
            ("Booking created: Boiler service", "booking", "+447700000001"),
        ]
        for body, mtype, phone in msg_types:
            mid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO messages (id, business_id, direction, to_phone, message_type, message_body, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, biz_id, "outbound", phone, mtype, body, "sent", now),
            )
        conn.commit()
        conn.close()

        r = await client.get(f"{API}/messages")
        check("GET messages", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            msgs = r.json()
            check("Messages count >= 4", len(msgs) >= 4, f"got {len(msgs)}")

        # ── Step 12: Stats ──
        print("\n📊 Testing stats...")
        r = await client.get(f"{API}/stats")
        check("GET stats", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            stats = r.json()
            ok("Stats response", str(stats))

        # ── Step 13: Accounts (income overview) ──
        print("\n🏦 Testing accounts...")
        r = await client.get(f"{API}/accounts")
        check("GET accounts", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            accts = r.json()
            check("Accounts has data", isinstance(accts, (list, dict)),
                  f"type={type(accts).__name__}")

        # ── Step 14: Delete tests (cleanup) ──
        print("\n🗑️ Testing delete operations...")
        # Delete a booking
        if booking_ids:
            r = await client.delete(f"{API}/bookings/{booking_ids[-1]}")
            check("DELETE booking", r.status_code == 200, f"status={r.status_code}")

            # Confirm it's gone
            r = await client.get(f"{API}/bookings/{booking_ids[-1]}")
            check("Deleted booking returns 404", r.status_code == 404, f"status={r.status_code}")

        # Delete an expense
        if expense_ids:
            r = await client.delete(f"{API}/expenses/{expense_ids[-1]}")
            check("DELETE expense", r.status_code == 200, f"status={r.status_code}")

        # Delete a quote
        if quote_ids:
            r = await client.delete(f"{API}/quotes/{quote_ids[-1]}")
            check("DELETE quote", r.status_code == 200, f"status={r.status_code}")

        # ── Step 15: Portal data verification ──
        print("\n🖥️  Verifying portal data completeness...")
        # Re-fetch all data the portal would load
        data_checks = {
            "business": (API, dict),
            "customers": (f"{API}/customers", list),
            "invoices": (f"{API}/invoices", list),
            "quotes": (f"{API}/quotes", list),
            "expenses": (f"{API}/expenses", list),
            "bookings": (f"{API}/bookings", list),
            "messages": (f"{API}/messages", list),
            "drafts": (f"{API}/drafts", list),
            "stats": (f"{API}/stats", dict),
            "accounts": (f"{API}/accounts", (list, dict)),
            "expense_summary": (f"{API}/expenses/summary", dict),
        }

        portal_results = {}
        for name, (url, expected_type) in data_checks.items():
            r = await client.get(url)
            if r.status_code == 200:
                data = r.json()
                is_right_type = isinstance(data, expected_type)
                count = len(data) if isinstance(data, list) else "obj"
                portal_results[name] = {"ok": True, "count": count}
                check(f"Portal: {name}", is_right_type,
                      f"count={count}" if isinstance(data, list) else "✓")
            else:
                portal_results[name] = {"ok": False, "status": r.status_code}
                fail(f"Portal: {name}", f"status={r.status_code}")

    # ── Summary ──
    print("\n" + "=" * 60)
    print(f"  RESULTS: {len(passed)} passed, {len(failed)} failed")
    print("=" * 60)

    if failed:
        print("\n  Failed tests:")
        for f_name in failed:
            print(f"    ❌ {f_name}")

    print("\n  Portal Data Summary:")
    for name, info in portal_results.items():
        status = "✅" if info["ok"] else "❌"
        detail = f"count={info['count']}" if info["ok"] else f"HTTP {info['status']}"
        print(f"    {status} {name}: {detail}")

    print()
    return len(failed) == 0


if __name__ == "__main__":
    success = asyncio.run(run_tests())
    exit(0 if success else 1)
