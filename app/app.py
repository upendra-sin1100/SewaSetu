# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintenance contract ended 31/01/2026.
"""

import os
import io
import csv
import uuid
import random
import hashlib
import configparser
from datetime import datetime, date, timedelta, timezone

# Shared business rules (pure Python, no Flask/DB dependency).
# In Docker: WORKDIR=/app so `from rules import` resolves to /app/rules.py.
# In tests: conftest.py at the repo root adds app/ to sys.path first.
from rules import is_overdue, IST  # noqa: E402

import requests
import psycopg2
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort, jsonify)
from fpdf import FPDF


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

app = Flask(__name__)
# Secret key and admin password come from environment variables.
# No in-repo defaults – missing vars raise KeyError at startup so a
# misconfigured deployment fails loudly instead of running with known secrets.
_secret_key   = os.environ.get("SEWASETU_SECRET_KEY")
_admin_pw     = os.environ.get("SEWASETU_ADMIN_PASSWORD")
if not _secret_key:
    raise RuntimeError(
        "SEWASETU_SECRET_KEY env var is not set. "
        "Set it in your .env.production (never commit the value)."
    )
if not _admin_pw:
    raise RuntimeError(
        "SEWASETU_ADMIN_PASSWORD env var is not set. "
        "Set it in your .env.production (never commit the value)."
    )
app.secret_key = _secret_key
app.config["PERMANENT_SESSION_LIFETIME"] = 300

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = _admin_pw

SMS_GATEWAY_URL = config.get("app", "sms_gateway_url")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

# Fix 5 – upload: honour the form's stated 5 MB / JPG+PDF promise.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg"}

# Fix 6 – Indic font for PDF generation.
# NotoSansBengali-Regular.ttf is committed under app/static/fonts/ with OFL.txt.
NOTO_BENGALI_FONT = os.path.join(BASE_DIR, "static", "fonts",
                                 "NotoSansBengali-Regular.ttf")

# Safety net: if Flask's own MAX_CONTENT_LENGTH ever gets set, make sure it is
# at least as large as our limit so we don't double-gate below our own cap.
app.config.setdefault("MAX_CONTENT_LENGTH", MAX_UPLOAD_BYTES + 1024)


@app.errorhandler(413)
def request_entity_too_large(e):
    """Replace Flask's default 413 page with a citizen-readable message."""
    flash("This file is too large. "
          "Please upload a JPG or PDF smaller than 5 MB.")
    return render_template("upload.html"), 413

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def get_db():
    return psycopg2.connect(
        host=config.get("database", "host"),
        port=config.get("database", "port"),
        dbname=config.get("database", "name"),
        user=config.get("database", "user"),
        password=config.get("database", "password"),
    )


def sanitize(value, maxlen=100):
    """Trim name to at most maxlen *characters* (not bytes).

    The DB column is VARCHAR(100) in PostgreSQL UTF-8 – that means 100
    characters, not 100 bytes.  The previous 20-byte slice silently
    truncated multi-byte scripts (Bengali, Assamese) mid-character.

    maxlen defaults to 100 to match the DB column.  If the stripped
    value exceeds maxlen characters, we raise ValueError with a
    human-readable message so the route can flash it to the citizen
    rather than silently mangling the name.
    """
    if value is None:
        return ""
    value = value.strip()
    if len(value) > maxlen:
        raise ValueError(
            "Name is too long (maximum %d characters). "
            "Please shorten the name and try again." % maxlen
        )
    return value


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


def deadline_remaining():
    delta = SCHEME_DEADLINE - datetime.now()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Something went wrong. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")

        # Fix 5a – no file chosen
        if f is None or f.filename == "":
            flash("Please choose a file first. "
                  "Tap \u2018Choose File\u2019, select your age proof, "
                  "then tap \u2018Upload \u0026 Continue\u2019.")
            return render_template("upload.html")

        # Fix 5b – extension check (case-insensitive; .jpg, .jpeg, .pdf)
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            flash("This file type is not accepted. "
                  "Please upload a JPG or PDF file.")
            return render_template("upload.html")

        # Fix 5c – size check: read up to limit+1 so we can detect over-limit
        #           without loading the entire file into memory first.
        content = f.read(MAX_UPLOAD_BYTES + 1)
        if len(content) == 0:
            flash("This file is empty. Please choose a different file.")
            return render_template("upload.html")
        if len(content) > MAX_UPLOAD_BYTES:
            flash("This file is too large. "
                  "Please upload a JPG or PDF smaller than 5 MB.")
            return render_template("upload.html")

        # Fix 5d – safe filename: use uuid + extension; never the user's name.
        safe_name = "%s_%s%s" % (session["verified_mobile"], uuid.uuid4().hex, ext)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, safe_name)
        with open(path, "wb") as out:
            out.write(content)
        session["doc_path"] = path
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    return render_template("declaration.html")


def handle_submission():
    # TODO: split this up some day. It grew. - RK, 09/2024
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    doc_path = session.get("doc_path", "")

    try:
        name = sanitize(data.get("applicant_name", ""))
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("form_step", step=1))
    village = data.get("village", "").strip()
    block = data.get("block", "").strip()
    gender = data.get("gender", "")
    marital = data.get("marital_status", "")
    husband_name = data.get("husband_name", "")
    husband_employer = data.get("husband_employer", "")
    bank_account = data.get("bank_account", "").strip()
    ifsc = data.get("ifsc", "").strip().upper()
    dob_raw = data.get("dob", "").strip()

    if not name or not village or not block or not bank_account or not dob_raw:
        flash("Something went wrong. Please try again.")
        return redirect(url_for("form_step", step=1))

    if marital == "Widowed":
        if not husband_name or not husband_employer:
            flash("Something went wrong. Please try again.")
            return redirect(url_for("form_step", step=1))

    # lenient date parsing to reduce rejections (CR-2024-117)
    dob = None
    for fmt in ("%m/%d/%Y", "%d/%m/%Y"):
        try:
            dob = datetime.strptime(dob_raw, fmt).date()
            break
        except ValueError:
            continue
    if dob is None:
        flash("Something went wrong. Please try again.")
        return redirect(url_for("form_step", step=1))

    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < MIN_AGE:
        flash("Applicant must be above %d years of age to be eligible." % MIN_AGE)
        return redirect(url_for("form_step", step=1))

    if datetime.now() > SCHEME_DEADLINE:
        flash("The application deadline has passed.")
        return redirect(url_for("index"))

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM applications WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] > 0:
        cur.close(); conn.close()
        flash("An application already exists for this mobile number. "
              "Duplicate applications are not permitted.")
        return redirect(url_for("index"))

    app_no = new_application_no()
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
           RETURNING id""",
        (app_no, name, mobile, dob, gender, marital, husband_name,
         husband_employer, village, block, bank_account, ifsc, doc_path,
         datetime.now()))
    new_id = cur.fetchone()[0]

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = dob.strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(portal_pass)))
    conn.commit()

    app.logger.info("generating acknowledgment pdf application=%d" % new_id)
    pdf_bytes = generate_acknowledgment(cur, new_id)
    ack_dir = os.path.join(UPLOAD_DIR, "ack")
    os.makedirs(ack_dir, exist_ok=True)
    with open(os.path.join(ack_dir, "%d.pdf" % new_id), "wb") as out:
        out.write(pdf_bytes)

    cur.close(); conn.close()
    # Fix 4b – post-submission ack download: set a session token so the
    # citizen can fetch their own PDF immediately after submitting, without
    # a separate status-portal login.  The token is validated (not popped)
    # by ack_pdf() so that Chrome's PDF viewer can fetch the URL twice
    # (once to render, once for the download button) without losing auth.
    # The token is scoped to this application id only.
    session["just_submitted_id"] = new_id
    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


def generate_acknowledgment(cur, app_id):
    cur.execute(
        "SELECT application_no, applicant_name, mobile, dob, village, block, "
        "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
        (app_id,),
    )
    row = cur.fetchone()
    # Apply the same RTPS deemed-approval display logic as the status page.
    # Row layout: [0..7 fields], submitted_at[8], status[9]
    display_status = row[9]
    if row[9] == "PENDING" and is_overdue(row[8], datetime.now(tz=IST), SLA_DAYS):
        display_status = "DEEMED_APPROVED"
    display_row = row[:9] + (display_status,)
    pdf = FPDF()
    pdf.add_page()

    # Fix 6 – embed Noto Sans Bengali so Assamese/Bengali names render correctly.
    # The built-in Helvetica font is latin-1 only; any codepoint > 127 was
    # silently replaced with '?' by the bare except branch below.
    # We load the TTF once; if the file is somehow missing, fall back to
    # Helvetica (PDF will still be generated, names may show '?').
    noto_ok = False
    if os.path.exists(NOTO_BENGALI_FONT):
        try:
            pdf.add_font(family="NotoSansBengali", fname=NOTO_BENGALI_FONT)
            # Try enabling HarfBuzz text shaping (needs uharfbuzz installed).
            # If uharfbuzz is absent or set_text_shaping() does not exist on
            # this fpdf2 build, we skip shaping – the font still embeds
            # correctly and Latin + common Bengali glyphs will render.
            try:
                pdf.set_text_shaping(use_shaping_engine=True)
            except Exception:
                pass  # shaping unavailable – font still embedded
            noto_ok = True
        except Exception:
            pass  # font file corrupt or add_font failed

    def _set_title_font(bold=False):
        style = "B" if bold else ""
        if noto_ok:
            pdf.set_font("NotoSansBengali", style=style, size=14 if bold else 11)
        else:
            pdf.set_font("Helvetica", style=style, size=14 if bold else 11)

    def _set_body_font(size=10):
        if noto_ok:
            pdf.set_font("NotoSansBengali", size=size)
        else:
            pdf.set_font("Helvetica", size=size)

    _set_title_font(bold=True)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", new_x="LMARGIN", new_y="NEXT", align="C")
    _set_title_font(bold=False)
    pdf.cell(0, 8, "Department of Social Welfare", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)
    # decorative border, as per approved letterhead design
    for i in range(0, 2000):
        x = 10 + (i % 190)
        pdf.line(x, 282, x + 0.5, 282)
        pdf.line(x, 12, x + 0.5, 12)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    _set_body_font(size=10)
    for label, val in zip(labels, display_row):
        pdf.cell(60, 8, label, border=1)
        pdf.cell(0, 8, str(val), border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    if noto_ok:
        pdf.set_font("NotoSansBengali", size=9)
    else:
        pdf.set_font("Helvetica", style="I", size=9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Something went wrong. Please try again.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in") or session.get("admin"):
        # Admin uses /admin/application/<id>; citizens must be logged in via status portal.
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT application_no, applicant_name, mobile, dob, village, block, "
        "bank_account, ifsc, status, submitted_at, decided_at "
        "FROM applications WHERE id = %s",
        (app_id,),
    )
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    # Fix 2 – IDOR: a citizen may only view their own application.
    if row[2] != session.get("portal_mobile"):
        abort(403)
    # If the file is still PENDING but the SLA has elapsed, show it as
    # DEEMED_APPROVED to the citizen (RTPS Act obligation).  The DB row is
    # NOT updated here; the nightly scheduler does that.
    display_status = row[8]
    if row[8] == "PENDING" and is_overdue(row[9], datetime.now(tz=IST), SLA_DAYS):
        display_status = "DEEMED_APPROVED"
    # Rebuild the row tuple with the corrected display status
    display_row = row[:8] + (display_status,) + row[9:]
    return render_template("application.html", a=display_row, app_id=app_id)


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    # Fix 2 – IDOR: require citizen login and ownership check.
    # Fix 4b – post-submission: honour just_submitted_id token so a citizen can
    # download their PDF immediately after submitting without a status-portal
    # login.  The token is NOT popped – browsers (especially Chrome's PDF
    # viewer) request the same URL twice: once to render and again when the
    # user clicks the download button.  Popping on the first request left the
    # second request unauthenticated, producing an Unconfirmed .crdownload.
    # Instead we read the token without consuming it and validate it against
    # the requested app_id.  The token is still scoped to one application id.
    just_submitted = session.get("just_submitted_id")
    if just_submitted == app_id:
        # Token present and matches: serve without further ownership check.
        pass
    elif not session.get("logged_in"):
        flash("Please login to view your acknowledgement PDF.")
        return redirect(url_for("status_login"))
    else:
        # Logged-in path: check ownership.
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT mobile FROM applications WHERE id = %s",
            (app_id,),
        )
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            abort(404)
        if not session.get("admin") and row[0] != session.get("portal_mobile"):
            cur.close(); conn.close()
            abort(403)
        cur.close(); conn.close()
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    download_name = "acknowledgement-%d.pdf" % app_id
    if not os.path.exists(path):
        conn2 = get_db()
        cur2  = conn2.cursor()
        data  = generate_acknowledgment(cur2, app_id)
        cur2.close(); conn2.close()
        return send_file(
            io.BytesIO(data),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=download_name,
        )
    return send_file(
        path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=download_name,
    )


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        conn = get_db()
        cur = conn.cursor()
        # Fix 3 – SQLi: parameterised queries only; no string interpolation.
        cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        if row:
            cur.execute(
                "SELECT dob FROM applications WHERE mobile = %s ORDER BY id LIMIT 1",
                (mobile,),
            )
            r2 = cur.fetchone()
            if r2:
                newpass = r2[0].strftime("%d%m%Y")
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(newpass), row[0]))
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (request.form.get("username") == ADMIN_USERNAME and
                request.form.get("password") == ADMIN_PASSWORD):
            session["logged_in"] = True
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    page = int(request.args.get("page", 1))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                "submitted_at FROM applications WHERE status = %s "
                "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                (status, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("admin_view.html", a=row, app_id=app_id)


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    # Fix 2 – IDOR: require admin flag, not just any logged-in session.
    if not session.get("admin"):
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'APPROVED', decided_at = %s "
                "WHERE id = %s", (datetime.now(), app_id))
    conn.commit()
    cur.close(); conn.close()
    flash("Application approved.")
    return redirect(url_for("admin_list"))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    # Fix 2 – IDOR: require admin flag, not just any logged-in session.
    if not session.get("admin"):
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'REJECTED', decided_at = %s "
                "WHERE id = %s", (datetime.now(), app_id))
    conn.commit()
    cur.close(); conn.close()
    flash("Application rejected.")
    return redirect(url_for("admin_list"))


# ---------------------------------------------------------------------------
# Unused / legacy
# ---------------------------------------------------------------------------

def export_to_excel(rows):
    # Was used for the monthly disbursement report before eKosh integration.
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["application_no", "name", "account", "ifsc", "amount"])
    for r in rows:
        writer.writerow(list(r) + ["250"])
    return out.getvalue()


def old_payment_gateway_callback(txn):
    # retained for reference; eChallan integration was descoped in Phase 2
    status = txn.get("STATUS")
    if status == "0300":
        return "SUCCESS"
    elif status == "0399":
        return "FAILED"
    return "PENDING"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
