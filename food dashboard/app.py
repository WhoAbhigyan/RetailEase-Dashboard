import os
import io
import base64
import datetime as dt
from functools import wraps

from flask import Flask, request, jsonify, send_file, make_response
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import qrcode
import pandas as pd
from dotenv import load_dotenv
from urllib.parse import urlencode

# --- Load env ---
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "mysql+pymysql://root:password@localhost:3306/smart_kiosk")
JWT_SECRET = os.getenv("JWT_SECRET", "change_this_secret")
DEFAULT_GST = float(os.getenv("OWNER_DEFAULT_GST", "0.18"))

# --- Flask app ---
app = Flask(__name__)
# In dev, allow React at localhost:3000; tighten for prod as needed
CORS(app, resources={r"/*": {"origins": "*"}})

# SQLAlchemy config (MySQL via PyMySQL)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# --- Models ---

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default="owner")

class Owner(db.Model):
    __tablename__ = "owner"
    id = db.Column(db.Integer, primary_key=True)
    shop_name = db.Column(db.String(120), default="My Shop")
    phone = db.Column(db.String(50), default="")
    gst_number = db.Column(db.String(50), default="")
    default_gst_rate = db.Column(db.Float, default=0.18)
    upi_vpa = db.Column(db.String(120), default="")  # e.g., shop@upi

class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, nullable=False, default=0)
    gst_rate = db.Column(db.Float, nullable=True)  # override per product

class Sale(db.Model):
    __tablename__ = "sales"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    subtotal = db.Column(db.Float, nullable=False)
    gst = db.Column(db.Float, nullable=False)
    grand_total = db.Column(db.Float, nullable=False)
    payment_mode = db.Column(db.String(20), nullable=False)
    invoice_no = db.Column(db.String(32), unique=True, nullable=False)

class SaleItem(db.Model):
    __tablename__ = "sale_items"
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    qty = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    gst_rate = db.Column(db.Float, nullable=False)

# --- Helpers ---

def jwt_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = None
        if auth:
            parts = auth.split(" ", 1)
            if len(parts) == 2 and parts.lower() == "bearer":
                token = parts[1].strip()
        if not token:
            return jsonify({"error": "Unauthorized"}), 401
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            request.user = payload
        except jwt.PyJWTError:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return wrapper

def get_owner_row():
    owner = Owner.query.first()
    if not owner:
        owner = Owner(default_gst_rate=DEFAULT_GST)
        db.session.add(owner)
        db.session.commit()
    return owner

def resolve_gst_rate(product: Product, owner: Owner) -> float:
    if product.gst_rate is not None:
        return float(product.gst_rate)
    return float(owner.default_gst_rate or 0.18)

def next_invoice_no():
    last = db.session.query(Sale).order_by(Sale.id.desc()).first()
    nid = (last.id + 1) if last else 1
    return f"INV-{nid:05d}"

# --- DB init ---
with app.app_context():
    db.create_all()
    if not User.query.first():
        u = User(phone="admin", password_hash=generate_password_hash("admin123"))
        db.session.add(u)
        db.session.commit()
    get_owner_row()

# --- Auth ---

@app.post("/auth/login")
def login():
    data = request.get_json(force=True)
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""
    user = User.query.filter_by(phone=phone).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid credentials"}), 401
    payload = {
        "uid": user.id,
        "phone": user.phone,
        "role": user.role,
        "exp": dt.datetime.utcnow() + dt.timedelta(hours=12),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    return jsonify({"user": {"id": user.id, "phone": user.phone, "role": user.role}, "token": token})

@app.get("/auth/me")
@jwt_required
def me():
    return jsonify({"id": request.user["uid"], "phone": request.user["phone"], "role": request.user["role"]})

# --- Owner ---

@app.get("/owner")
@jwt_required
def get_owner():
    o = get_owner_row()
    return jsonify({
        "shopName": o.shop_name,
        "phone": o.phone,
        "gstNumber": o.gst_number,
        "defaultGstRate": o.default_gst_rate,
        "upiVpa": o.upi_vpa
    })

@app.put("/owner")
@jwt_required
def update_owner():
    data = request.get_json(force=True)
    o = get_owner_row()
    o.shop_name = data.get("shopName", o.shop_name)
    o.phone = data.get("phone", o.phone)
    o.gst_number = data.get("gstNumber", o.gst_number)
    if "defaultGstRate" in data:
        try:
            o.default_gst_rate = float(data["defaultGstRate"])
        except Exception:
            pass
    o.upi_vpa = data.get("upiVpa", o.upi_vpa)
    db.session.commit()
    return jsonify({"ok": True})

# --- Products ---

@app.get("/products")
@jwt_required
def list_products():
    q = (request.args.get("q") or "").lower().strip()
    query = Product.query
    if q:
        query = query.filter(
            db.or_(Product.name.ilike(f"%{q}%"), Product.category.ilike(f"%{q}%"))
        )
    items = query.order_by(Product.id.desc()).all()
    return jsonify([
        {"id": p.id, "name": p.name, "category": p.category, "price": p.price, "stock": p.stock, "gstRate": p.gst_rate}
        for p in items
    ])

@app.post("/products")
@jwt_required
def create_product():
    data = request.get_json(force=True)
    p = Product(
        name=data["name"].strip(),
        category=data["category"].strip(),
        price=float(data["price"]),
        stock=int(data.get("stock", 0)),
        gst_rate=float(data["gstRate"]) if data.get("gstRate") not in (None, "",) else None
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"id": p.id, "name": p.name, "category": p.category, "price": p.price, "stock": p.stock, "gstRate": p.gst_rate})

@app.put("/products/<int:pid>")
@jwt_required
def update_product(pid):
    p = Product.query.get_or_404(pid)
    data = request.get_json(force=True)
    p.name = data.get("name", p.name)
    p.category = data.get("category", p.category)
    if "price" in data: p.price = float(data["price"])
    if "stock" in data: p.stock = int(data["stock"])
    if "gstRate" in data:
        val = data["gstRate"]
        p.gst_rate = float(val) if val not in (None, "",) else None
    db.session.commit()
    return jsonify({"id": p.id, "name": p.name, "category": p.category, "price": p.price, "stock": p.stock, "gstRate": p.gst_rate})

@app.delete("/products/<int:pid>")
@jwt_required
def delete_product(pid):
    p = Product.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})

# --- Billing ---

@app.post("/billing/sale")
@jwt_required
def create_sale():
    """
    Body:
    {
      "lines": [{ "productId": 1, "qty": 2, "price": 45.0 }],
      "paymentMode": "CASH" | "UPI"
    }
    """
    o = get_owner_row()
    data = request.get_json(force=True)
    lines_in = data.get("lines") or []
    payment_mode = (data.get("paymentMode") or "CASH").upper()
    if not lines_in:
        return jsonify({"error": "No lines"}), 400

    subtotal = 0.0
    total_gst = 0.0
    items_for_db = []
    for l in lines_in:
        p = Product.query.get(l["productId"])
        if not p:
            return jsonify({"error": f"Product {l['productId']} not found"}), 404
        qty = int(l.get("qty", 1))
        if qty <= 0:
            return jsonify({"error": "Invalid qty"}), 400
        if p.stock < qty:
            return jsonify({"error": f"Insufficient stock for {p.name}"}), 400
        price = float(l.get("price", p.price))
        rate = resolve_gst_rate(p, o)
        sub = price * qty
        gst = sub * rate
        subtotal += sub
        total_gst += gst
        items_for_db.append({"product": p, "qty": qty, "price": price, "rate": rate})

    grand_total = subtotal + total_gst

    sale = Sale(
        date=dt.date.today(),
        subtotal=subtotal,
        gst=total_gst,
        grand_total=grand_total,
        payment_mode=payment_mode,
        invoice_no=next_invoice_no()
    )
    db.session.add(sale)
    db.session.flush()

    for it in items_for_db:
        si = SaleItem(
            sale_id=sale.id,
            product_id=it["product"].id,
            name=it["product"].name,
            qty=it["qty"],
            price=it["price"],
            gst_rate=it["rate"]
        )
        db.session.add(si)
        it["product"].stock = max(0, it["product"].stock - it["qty"])
    db.session.commit()

    return jsonify({
        "id": sale.id,
        "invoiceNo": sale.invoice_no,
        "date": sale.date.isoformat(),
        "totals": {"subTotal": sale.subtotal, "gst": sale.gst, "grandTotal": sale.grand_total},
        "paymentMode": sale.payment_mode
    })

# --- UPI QR Generation ---

@app.post("/billing/upi-qr")
@jwt_required
def upi_qr():
    """
    Build a UPI intent and return a PNG QR as data URL.
    Body: { "amount": 123.45, "note": "Order INV-00001", "vpa": "optional@upi" }
    """
    o = get_owner_row()
    data = request.get_json(force=True)
    amount = float(data.get("amount", 0.0))
    note = (data.get("note") or "Payment")
    vpa = (data.get("vpa") or o.upi_vpa or "").strip()
    if not vpa:
        return jsonify({"error": "Owner UPI VPA not configured"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be > 0"}), 400

    # Required UPI intent params: pa, pn, am, cu=INR, tn
    params = {
        "pa": vpa,
        "pn": o.shop_name or "Shop",
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": note
    }
    intent = "upi://pay?" + urlencode(params)

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(intent)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"

    return jsonify({"intent": intent, "qrDataUrl": data_url})

# --- Reports ---

@app.get("/reports/summary")
@jwt_required
def reports_summary():
    r = (request.args.get("range") or "daily").lower()
    today = dt.date.today()

    def in_range(d):
        if r == "daily":
            return d == today
        if r == "weekly":
            return (today - d).days <= 7
        if r == "monthly":
            return (today - d).days <= 31
        return True

    rows = Sale.query.all()
    sel = [s for s in rows if in_range(s.date)]
    total = sum(s.grand_total for s in sel)
    items = 0
    gst = sum(s.gst for s in sel)
    if sel:
        ids = [s.id for s in sel]
        items = db.session.query(db.func.sum(SaleItem.qty)).filter(SaleItem.sale_id.in_(ids)).scalar() or 0

    trend = []
    for i in range(6, -1, -1):
        d = today - dt.timedelta(days=i)
        day_total = sum(s.grand_total for s in rows if s.date == d)
        trend.append({"day": d.strftime("%d %b"), "total": day_total})

    return jsonify({"total": total, "items": items, "gst": gst, "trend": trend, "today": {
        "total": sum(s.grand_total for s in rows if s.date == today),
        "items": db.session.query(db.func.sum(SaleItem.qty)).filter(
            SaleItem.sale_id.in_([s.id for s in rows if s.date == today])
        ).scalar() or 0,
        "gst": sum(s.gst for s in rows if s.date == today)
    }})

@app.get("/reports/csv")
@jwt_required
def reports_csv():
    r = (request.args.get("range") or "daily").lower()
    today = dt.date.today()

    def in_range(d):
        if r == "daily":
            return d == today
        if r == "weekly":
            return (today - d).days <= 7
        if r == "monthly":
            return (today - d).days <= 31
        return True

    sales = Sale.query.all()
    sel = [s for s in sales if in_range(s.date)]
    rows = []
    for s in sel:
        items = SaleItem.query.filter_by(sale_id=s.id).all()
        for it in items:
            rows.append({
                "Invoice": s.invoice_no,
                "Date": s.date.isoformat(),
                "Item": it.name,
                "Qty": it.qty,
                "Price": it.price,
                "GST%": f"{it.gst_rate*100:.0f}%",
                "Line Total": round(it.price * it.qty * (1+it.gst_rate), 2),
                "Payment": s.payment_mode
            })
    df = pd.DataFrame(rows)
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    return make_response((csv_bytes, 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="sales_{r}.csv"'
    }))

@app.get("/reports/excel")
@jwt_required
def reports_excel():
    r = (request.args.get("range") or "daily").lower()
    today = dt.date.today()

    def in_range(d):
        if r == "daily":
            return d == today
        if r == "weekly":
            return (today - d).days <= 7
        if r == "monthly":
            return (today - d).days <= 31
        return True

    sales = Sale.query.all()
    sel = [s for s in sales if in_range(s.date)]
    rows = []
    for s in sel:
        items = SaleItem.query.filter_by(sale_id=s.id).all()
        for it in items:
            rows.append({
                "Invoice": s.invoice_no,
                "Date": s.date.isoformat(),
                "Item": it.name,
                "Qty": it.qty,
                "Price": it.price,
                "GST%": f"{it.gst_rate*100:.0f}%",
                "Line Total": round(it.price * it.qty * (1+it.gst_rate), 2),
                "Payment": s.payment_mode
            })
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Sales", index=False)
    buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"sales_{r}.xlsx")

# --- Health ---

@app.get("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(debug=True)
