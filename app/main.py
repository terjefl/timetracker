"""Timetracker — timeregistrering og enkel fakturering for private oppdrag."""

import csv
import io
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db

BASE = Path(__file__).parent
VERSION = os.environ.get("APP_VERSION", "dev")[:7]

app = FastAPI(title="Timetracker", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

MONTHS = ["jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"]
STATUS_LABELS = {"kladd": "Kladd", "sendt": "Sendt", "betalt": "Betalt"}


@app.on_event("startup")
def startup() -> None:
    db.init_db()


# --------------------------------------------------------------------------
# Formatering og parsing
# --------------------------------------------------------------------------

def _group_thousands(s: str) -> str:
    return re.sub(r"(\d)(?=(\d{3})+$)", r"\1 ", s)


def fmt_money(value) -> str:
    value = round(value or 0, 2)
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    return f"{sign}{_group_thousands(whole)},{frac} kr"


def fmt_hours(value) -> str:
    value = round(value or 0, 2)
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    whole, _, frac = text.partition(".")
    return _group_thousands(whole) + (f",{frac}" if frac else "")


def fmt_rate(value) -> str:
    """Timepris uten øre når den er et heltall: «850 kr»."""
    value = value or 0
    if float(value).is_integer():
        return f"{_group_thousands(str(int(value)))} kr"
    return fmt_money(value)


def fmt_date(value) -> str:
    if not value:
        return ""
    d = date.fromisoformat(str(value)[:10])
    return d.strftime("%d.%m.%Y")


def parse_hours(raw: str) -> float:
    """Godtar «1,5», «1.5», «1:30» og «90m»."""
    raw = (raw or "").strip().lower().replace(" ", "")
    try:
        if ":" in raw:
            h, m = raw.split(":", 1)
            hours = int(h or 0) + int(m or 0) / 60
        elif raw.endswith("m"):
            hours = int(raw[:-1]) / 60
        else:
            hours = float(raw.rstrip("t").replace(",", "."))
    except ValueError:
        raise HTTPException(400, f"Ugyldig antall timer: «{raw}»")
    if not 0 < hours <= 24:
        raise HTTPException(400, "Antall timer må være mellom 0 og 24")
    return round(hours, 2)


def parse_money(raw: str) -> float:
    raw = (raw or "0").strip().replace(" ", "").replace("kr", "").replace(",", ".")
    try:
        value = float(raw or 0)
    except ValueError:
        raise HTTPException(400, f"Ugyldig beløp: «{raw}»")
    if value < 0:
        raise HTTPException(400, "Timepris kan ikke være negativ")
    return round(value, 2)


def parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise HTTPException(400, f"Ugyldig dato: «{raw}»")


templates.env.filters.update(money=fmt_money, hours=fmt_hours, rate=fmt_rate, date=fmt_date)
templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, VERSION=VERSION)
# Cache-busting for statiske filer: endres ved hver ny build/endring av CSS
templates.env.globals["ASSET_VERSION"] = (
    VERSION if VERSION != "dev" else str(int((BASE / "static" / "style.css").stat().st_mtime))
)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.method == "POST" and exc.status_code == 400:
        back = request.headers.get("referer") or "/"
        back = re.sub(r"[?&](msg|err)=[^&]*", "", back)
        sep = "&" if "?" in back else "?"
        from urllib.parse import quote
        return redirect(f"{back}{sep}err={quote(str(exc.detail))}")
    return render(request, "error.html", status=exc.status_code, detail=exc.detail)


# --------------------------------------------------------------------------
# Datatilgang
# --------------------------------------------------------------------------

def get_customer(conn, customer_id: int):
    c = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not c:
        raise HTTPException(404, "Fant ikke kunden")
    return c


def active_rates(conn, customer_id: int):
    return conn.execute(
        "SELECT * FROM rate_types WHERE customer_id = ? AND active = 1 ORDER BY sort_order, id",
        (customer_id,),
    ).fetchall()


def customers_with_rates(conn, include_inactive=False):
    """Kunder + aktive pristyper, til skjemaene (JSON for nedtrekkslista)."""
    where = "" if include_inactive else "WHERE active = 1"
    customers = conn.execute(f"SELECT * FROM customers {where} ORDER BY name COLLATE NOCASE").fetchall()
    result = []
    for c in customers:
        rates = [
            {"id": r["id"], "name": r["name"], "rate": r["hourly_rate"]}
            for r in active_rates(conn, c["id"])
        ]
        result.append({"id": c["id"], "name": c["name"], "rates": rates})
    return result


def resolve_rate(conn, customer_id: int, rate_type_id: str | None):
    """Velger pristype. Én type → den brukes alltid; ingen → uten pris."""
    rates = active_rates(conn, customer_id)
    if not rates:
        return None, "", 0.0
    chosen = rates[0]
    if rate_type_id and len(rates) > 1:
        match = [r for r in rates if str(r["id"]) == str(rate_type_id)]
        if not match:
            raise HTTPException(400, "Ugyldig pristype for denne kunden")
        chosen = match[0]
    return chosen["id"], chosen["name"], chosen["hourly_rate"]


def entry_query(customer_id=None, date_from=None, date_to=None, status="ufakturert", invoice_id=None):
    sql = """
        SELECT e.*, e.hours * e.hourly_rate AS amount, c.name AS customer_name,
               i.number AS invoice_number, i.status AS invoice_status
        FROM entries e
        JOIN customers c ON c.id = e.customer_id
        LEFT JOIN invoices i ON i.id = e.invoice_id
        WHERE 1 = 1
    """
    args: list = []
    if customer_id:
        sql += " AND e.customer_id = ?"
        args.append(customer_id)
    if date_from:
        sql += " AND e.work_date >= ?"
        args.append(date_from)
    if date_to:
        sql += " AND e.work_date <= ?"
        args.append(date_to)
    if invoice_id:
        sql += " AND e.invoice_id = ?"
        args.append(invoice_id)
    elif status == "ufakturert":
        sql += " AND e.invoice_id IS NULL"
    elif status == "fakturert":
        sql += " AND e.invoice_id IS NOT NULL"
    sql += " ORDER BY c.name COLLATE NOCASE, e.work_date, e.id"
    return sql, args


def summarize(entries):
    """Totaler og summer per pristype."""
    by_type: dict = {}
    for e in entries:
        key = (e["rate_name"] or "Uten pristype", e["hourly_rate"])
        t = by_type.setdefault(key, {"name": key[0], "rate": key[1], "hours": 0.0, "amount": 0.0})
        t["hours"] += e["hours"]
        t["amount"] += e["amount"]
    return {
        "hours": sum(e["hours"] for e in entries),
        "amount": sum(e["amount"] for e in entries),
        "count": len(entries),
        "by_type": sorted(by_type.values(), key=lambda t: -t["rate"]),
    }


def invoice_locked(entry) -> bool:
    return entry["invoice_id"] is not None and entry["invoice_status"] in ("sendt", "betalt")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    year_start = today.replace(month=1, day=1).isoformat()
    with db.get_db() as conn:
        per_customer = conn.execute(
            """
            SELECT c.id, c.name,
                   COALESCE(SUM(CASE WHEN e.invoice_id IS NULL THEN e.hours END), 0) AS unbilled_hours,
                   COALESCE(SUM(CASE WHEN e.invoice_id IS NULL THEN e.hours * e.hourly_rate END), 0) AS unbilled_amount,
                   COALESCE(SUM(CASE WHEN e.work_date >= :m THEN e.hours END), 0) AS month_hours,
                   COALESCE(SUM(CASE WHEN e.work_date >= :y THEN e.hours END), 0) AS year_hours,
                   COALESCE(SUM(e.hours), 0) AS total_hours,
                   COALESCE(SUM(e.hours * e.hourly_rate), 0) AS total_amount,
                   MAX(e.work_date) AS last_date
            FROM customers c
            LEFT JOIN entries e ON e.customer_id = c.id
            GROUP BY c.id
            HAVING c.active = 1 OR unbilled_hours > 0
            ORDER BY year_hours DESC, c.name COLLATE NOCASE
            """,
            {"m": month_start, "y": year_start},
        ).fetchall()

        # Timer per måned siste 12 måneder, stablet per kunde
        y, m = divmod(today.year * 12 + today.month - 1 - 11, 12)
        first = date(y, m + 1, 1)
        monthly_rows = conn.execute(
            """
            SELECT substr(work_date, 1, 7) AS ym, customer_id, SUM(hours) AS hours
            FROM entries WHERE work_date >= ? GROUP BY ym, customer_id
            """,
            (first.isoformat(),),
        ).fetchall()

        recent = conn.execute(
            """
            SELECT e.*, e.hours * e.hourly_rate AS amount, c.name AS customer_name,
                   i.number AS invoice_number, i.status AS invoice_status
            FROM entries e JOIN customers c ON c.id = e.customer_id
            LEFT JOIN invoices i ON i.id = e.invoice_id
            ORDER BY e.work_date DESC, e.id DESC LIMIT 8
            """
        ).fetchall()
        open_invoices = conn.execute(
            """
            SELECT i.*, c.name AS customer_name,
                   (SELECT SUM(hours * hourly_rate) FROM entries WHERE invoice_id = i.id) AS amount
            FROM invoices i JOIN customers c ON c.id = i.customer_id
            WHERE i.status != 'betalt' ORDER BY i.number DESC
            """
        ).fetchall()
        customers = customers_with_rates(conn)
        last_customer = conn.execute(
            "SELECT customer_id FROM entries ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # Bygg månedsakse og farge per kunde
    months = []
    d = first
    while d <= today:
        months.append(d.strftime("%Y-%m"))
        d = (d + timedelta(days=32)).replace(day=1)
    palette_index = {c["id"]: i % 6 for i, c in enumerate(per_customer)}
    names = {c["id"]: c["name"] for c in per_customer}
    chart = []
    for ym in months:
        parts = [
            {"customer": names.get(r["customer_id"], "?"), "hours": r["hours"],
             "color": palette_index.get(r["customer_id"], 0)}
            for r in monthly_rows if r["ym"] == ym
        ]
        y, m = ym.split("-")
        chart.append({"label": MONTHS[int(m) - 1], "year": y, "hours": sum(p["hours"] for p in parts),
                      "parts": parts, "current": ym == today.strftime("%Y-%m")})
    chart_max = max([c["hours"] for c in chart] + [1])

    totals = {
        "unbilled_hours": sum(c["unbilled_hours"] for c in per_customer),
        "unbilled_amount": sum(c["unbilled_amount"] for c in per_customer),
        "month_hours": sum(c["month_hours"] for c in per_customer),
        "year_hours": sum(c["year_hours"] for c in per_customer),
        "outstanding": sum(i["amount"] or 0 for i in open_invoices if i["status"] == "sendt"),
    }
    max_year = max([c["year_hours"] for c in per_customer] + [1])
    return render(
        request, "dashboard.html",
        per_customer=per_customer, palette_index=palette_index, totals=totals, max_year=max_year,
        chart=chart, chart_max=chart_max, recent=recent, open_invoices=open_invoices,
        customers=customers, customers_json=json.dumps(customers),
        default_customer=last_customer["customer_id"] if last_customer else None,
        today=today.isoformat(), month_name=MONTHS[today.month - 1], year=today.year,
    )


# --------------------------------------------------------------------------
# Kunder
# --------------------------------------------------------------------------

@app.get("/kunder", response_class=HTMLResponse)
def customer_list(request: Request, vis: str = "aktive"):
    with db.get_db() as conn:
        where = "" if vis == "alle" else "WHERE c.active = 1"
        customers = conn.execute(
            f"""
            SELECT c.*,
                   (SELECT COUNT(*) FROM rate_types r WHERE r.customer_id = c.id AND r.active = 1) AS rate_count,
                   (SELECT GROUP_CONCAT(name || ': ' || hourly_rate, '|') FROM
                       (SELECT name, hourly_rate FROM rate_types r WHERE r.customer_id = c.id AND r.active = 1
                        ORDER BY sort_order, id)) AS rate_list,
                   (SELECT COALESCE(SUM(hours), 0) FROM entries e WHERE e.customer_id = c.id AND e.invoice_id IS NULL) AS unbilled_hours
            FROM customers c {where}
            ORDER BY c.active DESC, c.name COLLATE NOCASE
            """
        ).fetchall()
        rates = {
            c["id"]: active_rates(conn, c["id"]) for c in customers
        }
    return render(request, "customers.html", customers=customers, rates=rates, vis=vis)


@app.get("/kunder/ny", response_class=HTMLResponse)
def customer_new(request: Request):
    return render(request, "customer_form.html", customer=None, rates=[], stats=None)


@app.post("/kunder/ny")
def customer_create(
    name: str = Form(...), phone: str = Form(""), email: str = Form(""),
    address: str = Form(""), notes: str = Form(""),
    rate_name: str = Form("Standard"), hourly_rate: str = Form(""),
):
    if not name.strip():
        raise HTTPException(400, "Navn må fylles ut")
    with db.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO customers(name, phone, email, address, notes) VALUES (?, ?, ?, ?, ?)",
            (name.strip(), phone.strip(), email.strip(), address.strip(), notes.strip()),
        )
        if hourly_rate.strip():
            conn.execute(
                "INSERT INTO rate_types(customer_id, name, hourly_rate) VALUES (?, ?, ?)",
                (cur.lastrowid, rate_name.strip() or "Standard", parse_money(hourly_rate)),
            )
    return redirect(f"/kunder/{cur.lastrowid}?msg=Kunden+er+opprettet")


@app.get("/kunder/{customer_id}", response_class=HTMLResponse)
def customer_edit(request: Request, customer_id: int):
    with db.get_db() as conn:
        customer = get_customer(conn, customer_id)
        rates = conn.execute(
            """
            SELECT r.*, (SELECT COUNT(*) FROM entries e WHERE e.rate_type_id = r.id) AS used
            FROM rate_types r WHERE r.customer_id = ? ORDER BY r.active DESC, r.sort_order, r.id
            """,
            (customer_id,),
        ).fetchall()
        stats = conn.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(hours), 0) AS hours,
                   COALESCE(SUM(CASE WHEN invoice_id IS NULL THEN hours END), 0) AS unbilled_hours,
                   COALESCE(SUM(CASE WHEN invoice_id IS NULL THEN hours * hourly_rate END), 0) AS unbilled_amount,
                   COALESCE(SUM(hours * hourly_rate), 0) AS amount
            FROM entries WHERE customer_id = ?
            """,
            (customer_id,),
        ).fetchone()
        invoices = conn.execute(
            """
            SELECT i.*, (SELECT SUM(hours * hourly_rate) FROM entries WHERE invoice_id = i.id) AS amount
            FROM invoices i WHERE customer_id = ? ORDER BY number DESC
            """,
            (customer_id,),
        ).fetchall()
    return render(request, "customer_form.html", customer=customer, rates=rates, stats=stats, invoices=invoices)


@app.post("/kunder/{customer_id}")
def customer_update(
    customer_id: int, name: str = Form(...), phone: str = Form(""), email: str = Form(""),
    address: str = Form(""), notes: str = Form(""),
):
    if not name.strip():
        raise HTTPException(400, "Navn må fylles ut")
    with db.get_db() as conn:
        get_customer(conn, customer_id)
        conn.execute(
            "UPDATE customers SET name=?, phone=?, email=?, address=?, notes=? WHERE id=?",
            (name.strip(), phone.strip(), email.strip(), address.strip(), notes.strip(), customer_id),
        )
    return redirect(f"/kunder/{customer_id}?msg=Lagret")


@app.post("/kunder/{customer_id}/aktiv")
def customer_toggle(customer_id: int):
    with db.get_db() as conn:
        c = get_customer(conn, customer_id)
        conn.execute("UPDATE customers SET active = ? WHERE id = ?", (0 if c["active"] else 1, customer_id))
    msg = "Kunden+er+arkivert" if c["active"] else "Kunden+er+aktiv+igjen"
    return redirect(f"/kunder/{customer_id}?msg={msg}")


@app.post("/kunder/{customer_id}/slett")
def customer_delete(customer_id: int):
    with db.get_db() as conn:
        get_customer(conn, customer_id)
        used = conn.execute(
            "SELECT (SELECT COUNT(*) FROM entries WHERE customer_id = ?) + "
            "(SELECT COUNT(*) FROM invoices WHERE customer_id = ?)",
            (customer_id, customer_id),
        ).fetchone()[0]
        if used:
            raise HTTPException(400, "Kunden har timer eller fakturaer og kan bare arkiveres")
        conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    return redirect("/kunder?msg=Kunden+er+slettet")


@app.post("/kunder/{customer_id}/priser")
def rate_create(customer_id: int, name: str = Form(...), hourly_rate: str = Form("0")):
    if not name.strip():
        raise HTTPException(400, "Pristypen må ha et navn")
    with db.get_db() as conn:
        get_customer(conn, customer_id)
        order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM rate_types WHERE customer_id = ?", (customer_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO rate_types(customer_id, name, hourly_rate, sort_order) VALUES (?, ?, ?, ?)",
            (customer_id, name.strip(), parse_money(hourly_rate), order),
        )
    return redirect(f"/kunder/{customer_id}?msg=Pristype+lagt+til#priser")


@app.post("/priser/{rate_id}")
def rate_update(rate_id: int, name: str = Form(...), hourly_rate: str = Form("0"), action: str = Form("lagre")):
    with db.get_db() as conn:
        rate = conn.execute("SELECT * FROM rate_types WHERE id = ?", (rate_id,)).fetchone()
        if not rate:
            raise HTTPException(404, "Fant ikke pristypen")
        cid = rate["customer_id"]
        if action == "slett":
            used = conn.execute("SELECT COUNT(*) FROM entries WHERE rate_type_id = ?", (rate_id,)).fetchone()[0]
            if used:
                # Historikken beholder navn og pris; typen skjules bare for nye timer.
                conn.execute("UPDATE rate_types SET active = 0 WHERE id = ?", (rate_id,))
                return redirect(f"/kunder/{cid}?msg=Pristypen+er+i+bruk+og+ble+deaktivert#priser")
            conn.execute("DELETE FROM rate_types WHERE id = ?", (rate_id,))
            return redirect(f"/kunder/{cid}?msg=Pristypen+er+slettet#priser")
        if action == "aktiver":
            conn.execute("UPDATE rate_types SET active = 1 WHERE id = ?", (rate_id,))
            return redirect(f"/kunder/{cid}?msg=Pristypen+er+aktiv+igjen#priser")
        if action in ("opp", "ned"):
            rates = conn.execute(
                "SELECT id FROM rate_types WHERE customer_id = ? AND active = 1 ORDER BY sort_order, id", (cid,)
            ).fetchall()
            ids = [r["id"] for r in rates]
            if rate_id in ids:
                i = ids.index(rate_id)
                j = i - 1 if action == "opp" else i + 1
                if 0 <= j < len(ids):
                    ids[i], ids[j] = ids[j], ids[i]
                for pos, rid in enumerate(ids):
                    conn.execute("UPDATE rate_types SET sort_order = ? WHERE id = ?", (pos, rid))
            return redirect(f"/kunder/{cid}#priser")
        if not name.strip():
            raise HTTPException(400, "Pristypen må ha et navn")
        conn.execute(
            "UPDATE rate_types SET name = ?, hourly_rate = ? WHERE id = ?",
            (name.strip(), parse_money(hourly_rate), rate_id),
        )
    return redirect(f"/kunder/{cid}?msg=Pristype+lagret.+Allerede+registrerte+timer+beholder+gammel+pris.#priser")


# --------------------------------------------------------------------------
# Timer
# --------------------------------------------------------------------------

@app.get("/timer", response_class=HTMLResponse)
def entry_list(request: Request, kunde: str = "", maaned: str = "", status: str = "alle"):
    customer_id = int(kunde) if kunde.isdigit() else None
    date_from = date_to = None
    if re.fullmatch(r"\d{4}-\d{2}", maaned):
        y, m = map(int, maaned.split("-"))
        date_from = date(y, m, 1).isoformat()
        date_to = ((date(y, m, 1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)).isoformat()
    with db.get_db() as conn:
        sql, args = entry_query(customer_id, date_from, date_to, status)
        sql = sql.replace("ORDER BY c.name COLLATE NOCASE, e.work_date, e.id", "ORDER BY e.work_date DESC, e.id DESC")
        entries = conn.execute(sql + " LIMIT 500", args).fetchall()
        customers = customers_with_rates(conn)
        all_customers = conn.execute("SELECT id, name FROM customers ORDER BY name COLLATE NOCASE").fetchall()
        months = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(work_date, 1, 7) FROM entries ORDER BY 1 DESC"
        )]
        last_customer = conn.execute("SELECT customer_id FROM entries ORDER BY id DESC LIMIT 1").fetchone()
    return render(
        request, "entries.html",
        entries=entries, summary=summarize(entries), customers=customers,
        customers_json=json.dumps(customers), all_customers=all_customers, months=months,
        filters={"kunde": kunde, "maaned": maaned, "status": status},
        default_customer=customer_id or (last_customer["customer_id"] if last_customer else None),
        today=date.today().isoformat(), month_label=lambda ym: f"{MONTHS[int(ym[5:])-1]} {ym[:4]}",
    )


@app.post("/timer")
def entry_create(
    customer_id: int = Form(...), work_date: str = Form(...), hours: str = Form(...),
    description: str = Form(""), rate_type_id: str = Form(""), next: str = Form("/timer"),
):
    with db.get_db() as conn:
        get_customer(conn, customer_id)
        rid, rname, rate = resolve_rate(conn, customer_id, rate_type_id)
        conn.execute(
            """INSERT INTO entries(customer_id, rate_type_id, rate_name, hourly_rate, work_date, hours, description)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (customer_id, rid, rname, rate, parse_date(work_date), parse_hours(hours), description.strip()),
        )
    target = next if next.startswith("/") and not next.startswith("//") else "/timer"
    sep = "&" if "?" in target else "?"
    return redirect(f"{target}{sep}msg=Timer+registrert")


@app.get("/timer/{entry_id}", response_class=HTMLResponse)
def entry_edit(request: Request, entry_id: int):
    with db.get_db() as conn:
        sql, args = entry_query(status="alle")
        entry = conn.execute(sql.replace("WHERE 1 = 1", "WHERE e.id = ?"), [entry_id]).fetchone()
        if not entry:
            raise HTTPException(404, "Fant ikke registreringen")
        customers = customers_with_rates(conn, include_inactive=True)
    return render(
        request, "entry_form.html", entry=entry, locked=invoice_locked(entry),
        customers=customers, customers_json=json.dumps(customers),
    )


@app.post("/timer/{entry_id}")
def entry_update(
    entry_id: int, customer_id: int = Form(...), work_date: str = Form(...), hours: str = Form(...),
    description: str = Form(""), rate_type_id: str = Form(""),
):
    with db.get_db() as conn:
        sql, _ = entry_query(status="alle")
        entry = conn.execute(sql.replace("WHERE 1 = 1", "WHERE e.id = ?"), [entry_id]).fetchone()
        if not entry:
            raise HTTPException(404, "Fant ikke registreringen")
        if invoice_locked(entry):
            raise HTTPException(400, "Timene er på en sendt faktura og kan ikke endres")
        if entry["invoice_id"] and customer_id != entry["customer_id"]:
            raise HTTPException(400, "Fjern timene fra fakturakladden før du bytter kunde")
        get_customer(conn, customer_id)
        keep_rate = customer_id == entry["customer_id"] and (
            not rate_type_id or str(rate_type_id) == str(entry["rate_type_id"] or "")
        )
        if keep_rate:
            # Uendret kunde og type: behold prisen som gjaldt da timene ble ført.
            rid, rname, rate = entry["rate_type_id"], entry["rate_name"], entry["hourly_rate"]
        else:
            rid, rname, rate = resolve_rate(conn, customer_id, rate_type_id)
        conn.execute(
            """UPDATE entries SET customer_id=?, rate_type_id=?, rate_name=?, hourly_rate=?,
                      work_date=?, hours=?, description=? WHERE id=?""",
            (customer_id, rid, rname, rate, parse_date(work_date), parse_hours(hours),
             description.strip(), entry_id),
        )
    return redirect("/timer?msg=Registreringen+er+oppdatert")


@app.post("/timer/{entry_id}/slett")
def entry_delete(entry_id: int):
    with db.get_db() as conn:
        sql, _ = entry_query(status="alle")
        entry = conn.execute(sql.replace("WHERE 1 = 1", "WHERE e.id = ?"), [entry_id]).fetchone()
        if not entry:
            raise HTTPException(404, "Fant ikke registreringen")
        if invoice_locked(entry):
            raise HTTPException(400, "Timene er på en sendt faktura og kan ikke slettes")
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    return redirect("/timer?msg=Registreringen+er+slettet")


# --------------------------------------------------------------------------
# Rapport
# --------------------------------------------------------------------------

def _report_data(conn, kunde: str, fra: str, til: str, status: str):
    customer_id = int(kunde) if str(kunde).isdigit() else None
    sql, args = entry_query(customer_id, parse_date(fra), parse_date(til), status)
    entries = conn.execute(sql, args).fetchall()
    groups: dict = {}
    for e in entries:
        groups.setdefault((e["customer_id"], e["customer_name"]), []).append(e)
    return customer_id, entries, [
        {"customer_id": cid, "name": name, "entries": rows, "summary": summarize(rows)}
        for (cid, name), rows in groups.items()
    ]


@app.get("/rapport", response_class=HTMLResponse)
def report(request: Request, kunde: str = "", fra: str = "", til: str = "", status: str = "ufakturert"):
    with db.get_db() as conn:
        customer_id, entries, groups = _report_data(conn, kunde, fra, til, status)
        customers = conn.execute(
            "SELECT id, name, active FROM customers ORDER BY active DESC, name COLLATE NOCASE"
        ).fetchall()
        customer = get_customer(conn, customer_id) if customer_id else None
        settings = db.get_settings(conn)
    return render(
        request, "report.html", groups=groups, total=summarize(entries), customers=customers,
        customer=customer, settings=settings,
        filters={"kunde": kunde, "fra": fra, "til": til, "status": status},
        can_invoice=bool(customer_id and status == "ufakturert" and entries),
        generated=datetime.now().strftime("%d.%m.%Y %H:%M"),
    )


@app.get("/rapport.csv")
def report_csv(kunde: str = "", fra: str = "", til: str = "", status: str = "ufakturert"):
    with db.get_db() as conn:
        _, entries, _ = _report_data(conn, kunde, fra, til, status)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Dato", "Kunde", "Type", "Timer", "Timepris", "Beløp", "Beskrivelse", "Faktura"])
    num = lambda v: f"{v:.2f}".replace(".", ",")
    for e in entries:
        w.writerow([fmt_date(e["work_date"]), e["customer_name"], e["rate_name"], num(e["hours"]),
                    num(e["hourly_rate"]), num(e["amount"]), e["description"], e["invoice_number"] or ""])
    w.writerow(["Sum", "", "", num(sum(e["hours"] for e in entries)), "",
                num(sum(e["amount"] for e in entries)), "", ""])
    filename = f"timer-{date.today().isoformat()}.csv"
    return Response(
        "﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# Fakturaer
# --------------------------------------------------------------------------

def get_invoice(conn, invoice_id: int):
    inv = conn.execute(
        "SELECT i.*, c.name AS customer_name FROM invoices i JOIN customers c ON c.id = i.customer_id WHERE i.id = ?",
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise HTTPException(404, "Fant ikke fakturaen")
    return inv


@app.post("/fakturaer")
def invoice_create(customer_id: int = Form(...), fra: str = Form(""), til: str = Form(""), note: str = Form("")):
    with db.get_db() as conn:
        get_customer(conn, customer_id)
        sql, args = entry_query(customer_id, parse_date(fra), parse_date(til), "ufakturert")
        ids = [r["id"] for r in conn.execute(sql, args)]
        if not ids:
            raise HTTPException(400, "Ingen ufakturerte timer i utvalget")
        settings = db.get_settings(conn)
        number = max(
            int(settings.get("next_invoice_number") or 1),
            conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM invoices").fetchone()[0],
        )
        today = date.today()
        due = today + timedelta(days=int(settings.get("payment_days") or 14))
        dates = conn.execute(
            f"SELECT MIN(work_date), MAX(work_date) FROM entries WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchone()
        cur = conn.execute(
            """INSERT INTO invoices(number, customer_id, invoice_date, due_date, period_from, period_to, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (number, customer_id, today.isoformat(), due.isoformat(), dates[0], dates[1], note.strip()),
        )
        conn.execute(
            f"UPDATE entries SET invoice_id = ? WHERE id IN ({','.join('?' * len(ids))})", [cur.lastrowid, *ids]
        )
        conn.execute("UPDATE settings SET value = ? WHERE key = 'next_invoice_number'", (str(number + 1),))
    return redirect(f"/fakturaer/{cur.lastrowid}?msg=Fakturakladd+opprettet.+Kontroller,+skriv+ut+og+marker+som+sendt.")


@app.get("/fakturaer", response_class=HTMLResponse)
def invoice_list(request: Request):
    with db.get_db() as conn:
        invoices = conn.execute(
            """
            SELECT i.*, c.name AS customer_name,
                   (SELECT SUM(hours) FROM entries WHERE invoice_id = i.id) AS hours,
                   (SELECT SUM(hours * hourly_rate) FROM entries WHERE invoice_id = i.id) AS amount
            FROM invoices i JOIN customers c ON c.id = i.customer_id
            ORDER BY i.number DESC
            """
        ).fetchall()
    return render(request, "invoices.html", invoices=invoices, today=date.today().isoformat())


@app.get("/fakturaer/{invoice_id}", response_class=HTMLResponse)
def invoice_view(request: Request, invoice_id: int):
    with db.get_db() as conn:
        inv = get_invoice(conn, invoice_id)
        customer = get_customer(conn, inv["customer_id"])
        sql, args = entry_query(invoice_id=invoice_id, status="alle")
        entries = conn.execute(sql, args).fetchall()
        settings = db.get_settings(conn)
    return render(
        request, "invoice.html", inv=inv, customer=customer, entries=entries,
        summary=summarize(entries), settings=settings,
    )


@app.post("/fakturaer/{invoice_id}/status")
def invoice_status(invoice_id: int, status: str = Form(...)):
    if status not in STATUS_LABELS:
        raise HTTPException(400, "Ugyldig status")
    now = datetime.now().isoformat(timespec="seconds")
    with db.get_db() as conn:
        get_invoice(conn, invoice_id)
        if status == "kladd":
            conn.execute("UPDATE invoices SET status='kladd', sent_at=NULL, paid_at=NULL WHERE id=?", (invoice_id,))
        elif status == "sendt":
            conn.execute(
                "UPDATE invoices SET status='sendt', sent_at=COALESCE(sent_at, ?), paid_at=NULL WHERE id=?",
                (now, invoice_id),
            )
        else:
            conn.execute(
                "UPDATE invoices SET status='betalt', sent_at=COALESCE(sent_at, ?), paid_at=? WHERE id=?",
                (now, now, invoice_id),
            )
    return redirect(f"/fakturaer/{invoice_id}?msg=Status:+{STATUS_LABELS[status]}")


@app.post("/fakturaer/{invoice_id}/notat")
def invoice_note(invoice_id: int, note: str = Form(""), invoice_date: str = Form(...), due_date: str = Form(...)):
    with db.get_db() as conn:
        inv = get_invoice(conn, invoice_id)
        if inv["status"] != "kladd":
            raise HTTPException(400, "Bare kladder kan endres")
        conn.execute(
            "UPDATE invoices SET note=?, invoice_date=?, due_date=? WHERE id=?",
            (note.strip(), parse_date(invoice_date), parse_date(due_date), invoice_id),
        )
    return redirect(f"/fakturaer/{invoice_id}?msg=Lagret")


@app.post("/fakturaer/{invoice_id}/fjern/{entry_id}")
def invoice_remove_entry(invoice_id: int, entry_id: int):
    with db.get_db() as conn:
        inv = get_invoice(conn, invoice_id)
        if inv["status"] != "kladd":
            raise HTTPException(400, "Bare kladder kan endres")
        conn.execute("UPDATE entries SET invoice_id = NULL WHERE id = ? AND invoice_id = ?", (entry_id, invoice_id))
    return redirect(f"/fakturaer/{invoice_id}?msg=Linjen+er+fjernet+fra+fakturaen")


@app.post("/fakturaer/{invoice_id}/slett")
def invoice_delete(invoice_id: int):
    with db.get_db() as conn:
        inv = get_invoice(conn, invoice_id)
        if inv["status"] != "kladd":
            raise HTTPException(400, "Bare kladder kan slettes. Sett fakturaen tilbake til kladd først.")
        conn.execute("UPDATE entries SET invoice_id = NULL WHERE invoice_id = ?", (invoice_id,))
        conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
        # Gi tilbake nummeret hvis det var det siste som ble brukt
        settings = db.get_settings(conn)
        if int(settings["next_invoice_number"]) == inv["number"] + 1:
            conn.execute("UPDATE settings SET value = ? WHERE key = 'next_invoice_number'", (str(inv["number"]),))
    return redirect("/fakturaer?msg=Fakturakladden+er+slettet.+Timene+er+ufakturert+igjen.")


# --------------------------------------------------------------------------
# Innstillinger
# --------------------------------------------------------------------------

@app.get("/innstillinger", response_class=HTMLResponse)
def settings_view(request: Request):
    with db.get_db() as conn:
        settings = db.get_settings(conn)
    return render(request, "settings.html", settings=settings)


@app.post("/innstillinger")
async def settings_save(request: Request):
    form = await request.form()
    with db.get_db() as conn:
        for key in db.DEFAULT_SETTINGS:
            if key in form:
                value = str(form[key]).strip()
                if key in ("payment_days", "next_invoice_number") and not value.isdigit():
                    raise HTTPException(400, "Betalingsfrist og fakturanummer må være heltall")
                conn.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
    return redirect("/innstillinger?msg=Innstillingene+er+lagret")


@app.get("/healthz")
def healthz():
    with db.get_db() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "version": VERSION}
