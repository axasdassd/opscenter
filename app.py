from flask import Flask, request, redirect, session, send_from_directory, render_template_string, make_response, url_for, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
import os, csv, secrets, uuid, json, re
from datetime import datetime, timedelta
from io import StringIO
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(24))
app.permanent_session_lifetime = timedelta(hours=12)

# ---------------- DATABASE SETUP ----------------

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'ops.db')}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8601658580:AAEF_qP8L-Nx2EEiymjg06Dt9dYQFsFX4Kc")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6797616764")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_NOTIFY_USER_ID = os.environ.get("SLACK_NOTIFY_USER_ID", "")  # Single Slack user to receive all notifications

# VAPID keys for Web Push
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS      = {"sub": os.environ.get("VAPID_MAILTO", "mailto:admin@opscenter.app")}

# ---------------- MODELS ----------------

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="op")
    last_login = db.Column(db.String(50), default="Never")
    status = db.Column(db.String(20), default="Active")
    slack_user_id = db.Column(db.String(50), nullable=True)  # NEW: Slack User ID
    logs = db.relationship("Log", backref="operator", lazy=True, foreign_keys="Log.user_id")

class Log(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    username_snapshot = db.Column(db.String(80))
    start_mileage = db.Column(db.Integer)
    end_mileage = db.Column(db.Integer)
    start_shift_time = db.Column(db.String(10))
    end_shift_time = db.Column(db.String(10))
    notes = db.Column(db.Text)
    submitted_at = db.Column(db.String(30), default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lat = db.Column(db.String(30))
    lng = db.Column(db.String(30))
    start_photo = db.Column(db.String(200), default="placeholder.jpg")
    end_mileage_photo = db.Column(db.String(200), default="placeholder.jpg")
    start_shift_photo = db.Column(db.String(200), default="placeholder.jpg")
    end_shift_photo = db.Column(db.String(200), default="placeholder.jpg")
    eta_img = db.Column(db.String(200), default="placeholder.jpg")

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    time = db.Column(db.String(30))
    event = db.Column(db.String(200))
    actor = db.Column(db.String(80), default="System")

class Break(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(20), nullable=False)
    timestamp = db.Column(db.String(30), default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    lat = db.Column(db.String(30))
    lng = db.Column(db.String(30))
    photo = db.Column(db.String(200), default='placeholder.jpg')

class Eta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    timestamp = db.Column(db.String(30), default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    lat = db.Column(db.String(30))
    lng = db.Column(db.String(30))
    photo = db.Column(db.String(200), default='placeholder.jpg')

class Address(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    address_text = db.Column(db.String(500), nullable=False)
    assigned_op_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_by_admin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    timestamp = db.Column(db.String(30), default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

# ── MESSAGING MODELS ──

class Channel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80))
    is_dm = db.Column(db.Boolean, default=False)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.String(30), default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    messages = db.relationship('Message', backref='channel', lazy=True, cascade='all, delete-orphan')
    members = db.relationship('ChannelMember', backref='channel', lazy=True, cascade='all, delete-orphan')

class ChannelMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.Integer, db.ForeignKey('channel.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    last_read_at = db.Column(db.String(30), default='1970-01-01 00:00:00')

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.Integer, db.ForeignKey('channel.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    sender_name = db.Column(db.String(80))
    body = db.Column(db.Text, nullable=False)
    sent_at = db.Column(db.String(30), default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    is_alert = db.Column(db.Boolean, default=False)

class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    subscription_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.String(30), default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

# ---------------- RATE LIMITER ----------------

limiter = Limiter(get_remote_address, app=app, default_limits=[])

# ---------------- SECURITY DECORATORS ----------------

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("role") != "admin":
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated_function

# ---------------- HELPERS ----------------

def log_event(event_text):
    e = Event(
        time=datetime.now().strftime("%Y-%m-%d %H:%M"),
        event=event_text,
        actor=session.get("username", "System")
    )
    db.session.add(e)
    all_events = Event.query.order_by(Event.id.desc()).all()
    for old in all_events[50:]:
        db.session.delete(old)


def send_slack_dm(slack_user_id, text, blocks=None):
    """Send a Slack DM to a user by their Slack User ID."""
    if not SLACK_BOT_TOKEN or not slack_user_id:
        return False
    try:
        import requests
        payload = {"channel": slack_user_id}
        if blocks:
            payload["blocks"] = blocks
        else:
            payload["text"] = text
        res = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json=payload,
            timeout=10
        )
        data = res.json()
        if not data.get("ok"):
            print(f"Slack DM error: {data.get('error')}")
        return data.get("ok", False)
    except Exception as ex:
        print(f"Slack DM failed: {ex}")
        return False


def notify_slack(message, filename=None):
    """Send notification to configured Slack user, optionally with a photo."""
    if not SLACK_NOTIFY_USER_ID:
        print("No SLACK_NOTIFY_USER_ID set")
        return

    if filename and filename != "placeholder.jpg" and os.path.exists(os.path.join(UPLOAD_FOLDER, filename)):
        print(f"Single file notification: {filename}")
        send_slack_dm_with_file(SLACK_NOTIFY_USER_ID, message, filename)
    else:
        if filename:
            print(f"File not found or placeholder, sending text-only: {filename}")
        print("Text-only notification")
        send_slack_dm(SLACK_NOTIFY_USER_ID, message)


def send_break_reminder(user_id, username):
    """Send break ending reminder to operator after 1 minute."""
    from threading import Timer
    def reminder():
        # Get user's Slack ID from database
        user = User.query.get(user_id)
        if user and user.slack_user_id:
            message = f"""⏰ Break Reminder
Hey {username}! Your 2-minute break is almost over.
You have 1 minute left. Please make sure to sign your end break at the opscenter site before returning to work."""
            send_slack_dm(user.slack_user_id, message)
            print(f"Break reminder sent to {username} ({user.slack_user_id})")
        else:
            print(f"No Slack ID for user {username}, cannot send break reminder")
    # Schedule reminder after 60 seconds (1 minute into the 2-minute break)
    Timer(60.0, reminder).start()
    print(f"Break reminder scheduled for {username} in 60 seconds")


def notify_slack_with_files(message, filenames):
    """Send notification with multiple photos - ENHANCED."""
    if not SLACK_NOTIFY_USER_ID:
        print("No SLACK_NOTIFY_USER_ID set")
        return

    valid_files = [f for f in filenames if f and f != "placeholder.jpg" and os.path.exists(os.path.join(UPLOAD_FOLDER, f))]
    print(f"Found {len(valid_files)} valid files: {valid_files}")

    if not valid_files:
        print("Sending text-only notification")
        send_slack_dm(SLACK_NOTIFY_USER_ID, message)
        return

    # Send first photo with full message, others as supplements
    for i, filename in enumerate(valid_files):
        caption = message if i == 0 else f"📎 Additional Photo {i+1}/{len(valid_files)}"
        success = send_slack_dm_with_file(SLACK_NOTIFY_USER_ID, caption, filename)
        if not success:
            print(f"Failed to send photo {i+1}: {filename}")
        else:
            print(f"Sent photo {i+1}: {filename}")


def get_slack_dm_channel(slack_user_id):
    """Open or get DM channel ID for a user."""
    if not SLACK_BOT_TOKEN or not slack_user_id:
        return None
    try:
        import requests
        res = requests.post(
            "https://slack.com/api/conversations.open",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
            json={"users": slack_user_id},
            timeout=10
        )
        data = res.json()
        print(f"DEBUG: conversations.open response: {data}")
        if data.get("ok") and data.get("channel"):
            channel_id = data["channel"]["id"]
            print(f"DEBUG: Got DM channel ID: {channel_id}")
            return channel_id
        print(f"DEBUG: Failed to open DM channel: {data.get('error')}")
        return None
    except Exception as ex:
        print(f"Error opening DM channel: {ex}")
        return None


def send_slack_dm_with_file(slack_user_id, caption, filename):
    """Send Slack DM with file attachment - Using new upload API."""
    if not SLACK_BOT_TOKEN or not slack_user_id:
        print("Missing credentials for file DM")
        return send_slack_dm(slack_user_id, caption)

    photo_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(photo_path):
        print(f"Photo not found: {photo_path}")
        return send_slack_dm(slack_user_id, f"{caption}\n\n[Photo missing: {filename}]")

    try:
        import requests

        # Step 1: Get DM channel
        print(f"Uploading {filename} to Slack DM {slack_user_id}")
        channel_id = get_slack_dm_channel(slack_user_id)
        if not channel_id:
            print("Could not open DM channel, sending text only")
            return send_slack_dm(slack_user_id, caption)

        # Step 2: Get upload URL using new API
        file_size = os.path.getsize(photo_path)
        upload_url_res = requests.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            data={"filename": filename, "length": file_size},
            timeout=15
        )
        upload_data = upload_url_res.json()
        print(f"getUploadURLExternal response: {upload_data}")

        if not upload_data.get("ok"):
            print(f"Failed to get upload URL: {upload_data.get('error')}")
            return send_slack_dm(slack_user_id, f"{caption}\n\nPhoto upload failed: {upload_data.get('error')}")

        upload_url = upload_data.get("upload_url")
        file_id = upload_data.get("file_id")

        if not upload_url or not file_id:
            print("Missing upload_url or file_id in response")
            return send_slack_dm(slack_user_id, caption)

        # Step 3: Upload file to the provided URL
        with open(photo_path, 'rb') as img:
            upload_res = requests.post(
                upload_url,
                data=img.read(),
                timeout=30
            )
        print(f"File upload to external URL status: {upload_res.status_code}")

        if upload_res.status_code != 200:
            print(f"Failed to upload file to external URL")
            return send_slack_dm(slack_user_id, f"{caption}\n\nPhoto upload failed")

        # Step 4: Complete the upload to send to channel
        complete_res = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
            json={
                "files": [{"id": file_id, "title": filename}],
                "channel_id": channel_id,
                "initial_comment": caption
            },
            timeout=15
        )
        complete_data = complete_res.json()
        print(f"completeUploadExternal response: {complete_data}")

        if complete_data.get("ok"):
            print(f"File uploaded successfully to {channel_id}")
            return True
        else:
            error_msg = complete_data.get('error', 'Unknown error')
            print(f"Complete upload failed: {error_msg}")
            return send_slack_dm(slack_user_id, f"{caption}\n\nPhoto upload failed: {error_msg}")

    except Exception as ex:
        print(f"File upload exception: {ex}")
        return send_slack_dm(slack_user_id, f"{caption}\n\nUpload error occurred")


def send_telegram_alert(action, filename, lat=None, lng=None, timestamp=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        caption = f"{action} break by {session.get('username', 'unknown')} at {timestamp or datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nLocation: {lat or 'N/A'}, {lng or 'N/A'}"
        photo_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(photo_path):
            return False
        import requests
        with open(photo_path, 'rb') as img:
            res = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption},
                files={'photo': img},
                timeout=30
            )
        return res.status_code == 200
    except Exception as ex:
        print(f"Telegram send failed: {ex}")
        return False


def check_daily_completion(user_id, target_date=None):
    if target_date is None:
        target_date = datetime.now().strftime('%Y-%m-%d')
    log_today = Log.query.filter(
        Log.user_id == user_id,
        Log.submitted_at.like(f"{target_date}%")
    ).first()
    breaks_today = Break.query.filter(
        Break.user_id == user_id,
        Break.timestamp.like(f"{target_date}%")
    ).all()
    has_start = any(b.action == 'Start' for b in breaks_today)
    has_end = any(b.action == 'End' for b in breaks_today)
    eta_today = Eta.query.filter(
        Eta.user_id == user_id,
        Eta.timestamp.like(f"{target_date}%")
    ).first()
    return {
        'has_log': bool(log_today),
        'has_break_start': has_start,
        'has_break_end': has_end,
        'has_eta': bool(eta_today),
        'is_complete': bool(log_today and has_start and has_end and eta_today),
        'log': log_today,
        'breaks': breaks_today,
        'eta': eta_today
    }


def send_transmission(user_id, completion_data):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        import requests
        user = User.query.get(user_id)
        if not user:
            return False
        log = completion_data['log']
        breaks = completion_data['breaks']
        eta = completion_data['eta']
        address = Address.query.filter_by(assigned_op_id=user_id).first()
        break_start = next((b for b in breaks if b.action == 'Start'), None)
        break_end = next((b for b in breaks if b.action == 'End'), None)
        shift_duration = ""
        if log.start_shift_time and log.end_shift_time:
            try:
                start = datetime.strptime(log.start_shift_time, '%H:%M')
                end = datetime.strptime(log.end_shift_time, '%H:%M')
                if end < start:
                    end += timedelta(days=1)
                duration = end - start
                hours, remainder = divmod(duration.seconds, 3600)
                minutes = remainder // 60
                shift_duration = f"{hours}h {minutes:02d}m"
            except:
                shift_duration = "N/A"
        break_duration = ""
        if break_start and break_end:
            try:
                start_dt = datetime.strptime(break_start.timestamp, '%Y-%m-%d %H:%M:%S')
                end_dt = datetime.strptime(break_end.timestamp, '%Y-%m-%d %H:%M:%S')
                duration = end_dt - start_dt
                minutes = duration.seconds // 60
                break_duration = f"{minutes} min"
            except:
                break_duration = "N/A"
        distance = ""
        if log.start_mileage and log.end_mileage:
            try:
                distance = str(log.end_mileage - log.start_mileage)
            except:
                distance = "N/A"
        try:
            date_obj = datetime.strptime(log.submitted_at, '%Y-%m-%d %H:%M:%S')
            formatted_date = date_obj.strftime('%A, %b %d %Y')
            filed_time = date_obj.strftime('%H:%M')
        except:
            formatted_date = log.submitted_at.split(' ')[0]
            filed_time = log.submitted_at.split(' ')[1][:5]
        message = f"""TRANSMISSION COMPLETE
{user.username.upper()} | {formatted_date} {filed_time}

ASSIGNMENT: {address.address_text if address else 'No assignment'}

TELEMETRY: {log.start_mileage or 'N/A'} -> {log.end_mileage or 'N/A'} mi ({distance} mi)
SHIFT: {log.start_shift_time or 'N/A'}-{log.end_shift_time or 'N/A'} ({shift_duration})
GPS: {log.lat or 'N/A'}, {log.lng or 'N/A'}

BREAK: {break_start.timestamp.split(' ')[1][:5] if break_start else 'N/A'}-{break_end.timestamp.split(' ')[1][:5] if break_end else 'N/A'} ({break_duration})

ETA: {eta.timestamp.split(' ')[1][:5] if eta else 'N/A'} | GPS: {eta.lat or 'N/A'}, {eta.lng or 'N/A'} if eta else 'N/A'

Photos: 7 attached"""
        res = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'},
            timeout=30
        )
        if res.status_code != 200:
            return False
        photo_fields = [
            ('Start Odometer', log.start_photo),
            ('End Odometer', log.end_mileage_photo),
            ('Shift In', log.start_shift_photo),
            ('Shift Out', log.end_shift_photo),
            ('Break Start', break_start.photo if break_start else None),
            ('Break End', break_end.photo if break_end else None),
            ('ETA Proof', eta.photo if eta else None)
        ]
        for label, photo_filename in photo_fields:
            if photo_filename and photo_filename != 'placeholder.jpg':
                photo_path = os.path.join(UPLOAD_FOLDER, photo_filename)
                if os.path.exists(photo_path):
                    try:
                        with open(photo_path, 'rb') as img:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                                data={'chat_id': TELEGRAM_CHAT_ID, 'caption': label},
                                files={'photo': img},
                                timeout=30
                            )
                    except:
                        pass
        return True
    except Exception as ex:
        print(f"Transmission failed: {ex}")
        return False


def trigger_transmission_check(user_id):
    completion = check_daily_completion(user_id)
    if completion['is_complete']:
        success = send_transmission(user_id, completion)
        if success:
            log_event(f"Transmission sent for user_id {user_id}")
        return success
    return False


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_file(file):
    if not file or file.filename == "":
        return None
    if not allowed_file(file.filename):
        return None
    ext = os.path.splitext(file.filename)[1].lower()
    filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}{ext}"
    file.save(os.path.join(UPLOAD_FOLDER, filename))
    return filename


def resolve_image_url(src):
    if isinstance(src, str) and (src.startswith("http://") or src.startswith("https://")):
        return src
    return url_for("uploaded_file", filename=src)

app.jinja_env.globals["resolve_image_url"] = resolve_image_url


def send_push_to_user(user_id, title, body_text, url="/"):
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return
    try:
        from pywebpush import webpush, WebPushException
        subs = PushSubscription.query.filter_by(user_id=user_id).all()
        dead = []
        for sub in subs:
            try:
                sub_data = json.loads(sub.subscription_json)
                webpush(
                    subscription_info=sub_data,
                    data=json.dumps({"title": title, "body": body_text, "url": url}),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims=VAPID_CLAIMS
                )
            except WebPushException as e:
                status = getattr(e.response, 'status_code', None)
                if status in (404, 410):
                    dead.append(sub)
            except Exception:
                pass
        for d in dead:
            db.session.delete(d)
        if dead:
            db.session.commit()
    except ImportError:
        pass


def get_or_create_general_channel():
    ch = Channel.query.filter_by(name="general", is_dm=False).first()
    if not ch:
        ch = Channel(name="general", is_dm=False, created_by=None)
        db.session.add(ch)
        db.session.flush()
    existing_ids = {m.user_id for m in ChannelMember.query.filter_by(channel_id=ch.id).all()}
    for u in User.query.all():
        if u.id not in existing_ids:
            db.session.add(ChannelMember(channel_id=ch.id, user_id=u.id))
    db.session.commit()
    return ch


def get_dm_channel(user_a_id, user_b_id):
    a_channels = {m.channel_id for m in ChannelMember.query.filter_by(user_id=user_a_id).all()}
    b_channels = {m.channel_id for m in ChannelMember.query.filter_by(user_id=user_b_id).all()}
    shared = a_channels & b_channels
    for cid in shared:
        ch = Channel.query.get(cid)
        if ch and ch.is_dm:
            return ch
    ch = Channel(name=None, is_dm=True, created_by=user_a_id)
    db.session.add(ch)
    db.session.flush()
    db.session.add(ChannelMember(channel_id=ch.id, user_id=user_a_id))
    db.session.add(ChannelMember(channel_id=ch.id, user_id=user_b_id))
    db.session.commit()
    return ch


def get_unread_count(user_id):
    total = 0
    memberships = ChannelMember.query.filter_by(user_id=user_id).all()
    for m in memberships:
        count = Message.query.filter(
            Message.channel_id == m.channel_id,
            Message.sent_at > m.last_read_at,
            Message.sender_id != user_id
        ).count()
        total += count
    return total


def parse_mentions(body):
    return re.findall(r'@(\w+)', body)

# ---------------- INIT DB ----------------

def init_db():
    with app.app_context():
        db.create_all()
        # Add slack_user_id column if it doesn't exist yet (safe migration)
        try:
            with db.engine.connect() as conn:
                # Check if column exists first
                if 'postgresql' in str(db.engine.url).lower():
                    # PostgreSQL
                    result = conn.execute(db.text("""
                        SELECT column_name FROM information_schema.columns 
                        WHERE table_name = 'user' AND column_name = 'slack_user_id'
                    """))
                    if not result.fetchone():
                        conn.execute(db.text("ALTER TABLE \"user\" ADD COLUMN slack_user_id VARCHAR(50)"))
                else:
                    # SQLite
                    result = conn.execute(db.text("PRAGMA table_info(user)"))
                    columns = [row[1] for row in result.fetchall()]
                    if 'slack_user_id' not in columns:
                        conn.execute(db.text("ALTER TABLE user ADD COLUMN slack_user_id VARCHAR(50)"))
                conn.commit()
        except Exception as e:
            print(f"Migration info: {e}")
            pass  # Column already exists or migration failed
        if not User.query.first():
            admin = User(
                username="admin",
                password=generate_password_hash("admin"),
                role="admin"
            )
            db.session.add(admin)
            db.session.add(Event(time=datetime.now().strftime("%Y-%m-%d %H:%M"), event="System Initialized"))
            db.session.commit()
            print("Default admin created. Username: admin | Password: admin")
            print("IMPORTANT: Change your admin password after first login.")
        get_or_create_general_channel()

init_db()

# ---------------- ROUTES ----------------

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("admin") if session["role"] == "admin" else url_for("op"))
    return render_template_string(LOGIN_HTML)

@app.route("/login", methods=["POST"])
@limiter.limit("10 per minute")
def login():
    u = request.form.get("username", "").strip()
    p = request.form.get("password", "").strip()
    user = User.query.filter_by(username=u).first()
    if user and check_password_hash(user.password, p):
        user.last_login = datetime.now().strftime("%b %d, %y | %H:%M")
        db.session.commit()
        session.permanent = True
        session["user_id"] = user.id
        session["username"] = user.username
        session["role"] = user.role
        get_or_create_general_channel()
        return redirect(url_for("admin") if user.role == "admin" else url_for("op"))
    return redirect(url_for("index"))

@app.route("/create_user", methods=["POST"])
@admin_required
def create_user():
    new_u = request.form.get("username", "").strip()
    new_p = request.form.get("password", "").strip()
    if not new_u or not new_p:
        return "Missing Credentials", 400
    if len(new_u) > 80 or len(new_p) > 255:
        return "Input too long", 400
    if User.query.filter(db.func.lower(User.username) == new_u.lower()).first():
        return "Operator already exists", 400
    user = User(username=new_u, password=generate_password_hash(new_p), role="op")
    db.session.add(user)
    db.session.flush()
    log_event(f"Created operator: {new_u}")
    db.session.commit()
    get_or_create_general_channel()
    return redirect(url_for("admin"))

@app.route("/update_user", methods=["POST"])
@login_required
def update_user():
    target_u = request.form.get("target_username")
    new_u = request.form.get("new_username", "").strip()
    new_p = request.form.get("new_password", "").strip()
    new_slack = request.form.get("slack_user_id", "").strip()
    if session["role"] != "admin" and target_u != session["username"]:
        return "Unauthorized", 403
    user = User.query.filter_by(username=target_u).first()
    if not user:
        return "User not found", 404
    if new_u:
        if len(new_u) > 80:
            return "Username too long", 400
        log_event(f"Renamed {user.username} to {new_u}")
        user.username = new_u
        if target_u == session["username"]:
            session["username"] = new_u
    if new_p:
        user.password = generate_password_hash(new_p)
        log_event(f"Updated credentials for {user.username}")
    if new_slack:
        user.slack_user_id = new_slack
        log_event(f"Updated Slack ID for {user.username}")
    db.session.commit()
    return redirect(url_for("admin") if session["role"] == "admin" else url_for("op"))

@app.route("/delete_user/<username>", methods=["POST"])
@admin_required
def delete_user(username):
    if username == session["username"]:
        return "Cannot delete your own account", 400
    user = User.query.filter_by(username=username).first()
    if user:
        log_event(f"Purged operator: {username}")
        db.session.delete(user)
        db.session.commit()
    return redirect(url_for("admin"))

@app.route("/delete_log/<int:log_id>", methods=["POST"])
@admin_required
def delete_log(log_id):
    record = Log.query.get(log_id)
    if record:
        log_event(f"Deleted log record #{log_id} for {record.username_snapshot}")
        db.session.delete(record)
        db.session.commit()
    return redirect(url_for("admin"))

@app.route("/delete_break/<int:break_id>", methods=["POST"])
@admin_required
def delete_break(break_id):
    record = Break.query.get(break_id)
    if record:
        log_event(f"Deleted break record #{break_id} for user_id {record.user_id}")
        db.session.delete(record)
        db.session.commit()
    return redirect(url_for("admin"))

@app.route("/delete_eta/<int:eta_id>", methods=["POST"])
@admin_required
def delete_eta(eta_id):
    record = Eta.query.get(eta_id)
    if record:
        log_event(f"Deleted ETA record #{eta_id} for user_id {record.user_id}")
        db.session.delete(record)
        db.session.commit()
    return redirect(url_for("admin"))

@app.route("/edit_log/<int:log_id>", methods=["POST"])
@admin_required
def edit_log(log_id):
    record = Log.query.get_or_404(log_id)
    form = request.form
    files = request.files
    try:
        record.start_mileage = int(form.get("start_mileage", record.start_mileage))
    except (ValueError, TypeError):
        pass
    try:
        record.end_mileage = int(form.get("end_mileage", record.end_mileage))
    except (ValueError, TypeError):
        pass
    record.start_shift_time = form.get("start_shift_time") or record.start_shift_time
    record.end_shift_time = form.get("end_shift_time") or record.end_shift_time
    record.notes = form.get("notes", record.notes)
    record.lat = form.get("lat", record.lat)
    record.lng = form.get("lng", record.lng)
    for field in ["start_photo", "end_mileage_photo", "start_shift_photo", "end_shift_photo", "eta_img"]:
        new_file = save_file(files.get(field))
        if new_file:
            setattr(record, field, new_file)
    log_event(f"Edited log record #{record.id} for {record.username_snapshot}")
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/edit_break/<int:break_id>", methods=["POST"])
@admin_required
def edit_break(break_id):
    record = Break.query.get_or_404(break_id)
    form = request.form
    new_photo = save_file(request.files.get("photo"))
    if new_photo:
        record.photo = new_photo
    record.action = form.get("action", record.action)
    record.lat = form.get("lat", record.lat)
    record.lng = form.get("lng", record.lng)
    ts_value = form.get("timestamp")
    if ts_value:
        try:
            record.timestamp = datetime.strptime(ts_value, "%Y-%m-%dT%H:%M").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    log_event(f"Edited break record #{record.id} for user_id {record.user_id}")
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/edit_eta/<int:eta_id>", methods=["POST"])
@admin_required
def edit_eta(eta_id):
    record = Eta.query.get_or_404(eta_id)
    form = request.form
    new_photo = save_file(request.files.get("photo"))
    if new_photo:
        record.photo = new_photo
    record.lat = form.get("lat", record.lat)
    record.lng = form.get("lng", record.lng)
    ts_value = form.get("timestamp")
    if ts_value:
        try:
            record.timestamp = datetime.strptime(ts_value, "%Y-%m-%dT%H:%M").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    log_event(f"Edited ETA record #{record.id} for user_id {record.user_id}")
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/submit", methods=["POST"])
@login_required
def submit():
    f, fls = request.form, request.files
    try:
        s_m = int(f.get("start_mileage", 0))
        e_m = int(f.get("end_mileage", 0))
    except ValueError:
        return "Invalid mileage data", 400
    log = Log(
        user_id=session["user_id"],
        username_snapshot=session["username"],
        start_mileage=s_m,
        end_mileage=e_m,
        start_shift_time=f.get("start_shift_time"),
        end_shift_time=f.get("end_shift_time"),
        notes=f.get("eta", "No notes"),
        lat=f.get("lat"),
        lng=f.get("lng"),
        start_photo=save_file(fls.get("start_photo")) or "placeholder.jpg",
        end_mileage_photo=save_file(fls.get("end_mileage_photo")) or "placeholder.jpg",
        start_shift_photo=save_file(fls.get("start_shift_photo")) or "placeholder.jpg",
        end_shift_photo=save_file(fls.get("end_shift_photo")) or "placeholder.jpg",
        eta_img=save_file(fls.get("eta_img")) or "placeholder.jpg"
    )
    db.session.add(log)
    db.session.commit()
    # Send Slack notification with all photos
    try:
        distance = e_m - s_m
        message = f"""📊 Daily Telemetry Complete
Operator: {session['username']}
Mileage: {s_m} → {e_m} ({distance} mi)
Shift: {f.get('start_shift_time')} - {f.get('end_shift_time')}
All photos uploaded"""
        photos = [log.start_photo, log.end_mileage_photo, log.start_shift_photo, log.end_shift_photo, log.eta_img]
        notify_slack_with_files(message, photos)
    except Exception as e:
        print(f"Slack notification error: {e}")
    trigger_transmission_check(session["user_id"])
    return render_template_string(SUCCESS_HTML)

@app.route("/break_action", methods=["POST"])
@login_required
def break_action():
    f = request.form
    fls = request.files
    action = f.get("action")
    if action not in ["Start", "End"]:
        return "Invalid break action", 400
    photo = fls.get("photo")
    if not photo:
        return "Photo required", 400
    filename = save_file(photo)
    if not filename:
        return "Invalid photo", 400
    lat = f.get("lat") or "N/A"
    lng = f.get("lng") or "N/A"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    brk = Break(
        user_id=session['user_id'],
        action=action,
        timestamp=timestamp,
        lat=lat,
        lng=lng,
        photo=filename
    )
    db.session.add(brk)
    log_event(f"{action} break by {session.get('username', 'unknown')} at {timestamp} [{lat}, {lng}]")
    db.session.commit()
    # Send Slack notification
    try:
        if action == "Start":
            notify_slack(f"""☕ Break Started
Operator: {session['username']}
Time: {timestamp.split(' ')[1]}
Location: {lat}, {lng}""", filename)
            # Schedule break reminder after 1 minute
            send_break_reminder(session['user_id'], session['username'])
        else:
            notify_slack(f"""🏁 Break Ended
Operator: {session['username']}
Time: {timestamp.split(' ')[1]}
Location: {lat}, {lng}""", filename)
    except Exception as e:
        print(f"Slack notification error: {e}")
    if action == "End":
        trigger_transmission_check(session['user_id'])
    return redirect(url_for("op"))

@app.route("/submit_eta", methods=["POST"])
@login_required
def submit_eta():
    f = request.form
    fls = request.files
    photo = fls.get("photo")
    if not photo:
        return "Photo required", 400
    filename = save_file(photo)
    if not filename:
        return "Invalid photo", 400
    lat = f.get("lat") or "N/A"
    lng = f.get("lng") or "N/A"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    eta_record = Eta(
        user_id=session['user_id'],
        timestamp=timestamp,
        lat=lat,
        lng=lng,
        photo=filename
    )
    db.session.add(eta_record)
    log_event(f"ETA photo submitted by {session.get('username', 'unknown')} at {timestamp} [{lat}, {lng}]")
    db.session.commit()
    # Send Slack notification
    try:
        notify_slack(f"""📍 ETA Location Submitted
Operator: {session['username']}
Time: {timestamp.split(' ')[1]}
Location: {lat}, {lng}""", filename)
    except Exception as e:
        print(f"Slack notification error: {e}")
    trigger_transmission_check(session['user_id'])
    return redirect(url_for("op"))

@app.route("/set_address", methods=["POST"])
@admin_required
def set_address():
    addr_text = request.form.get("address_text", "").strip()
    op_id = request.form.get("op_id")
    booking_time = request.form.get("booking_time", "").strip()
    if not addr_text or not op_id:
        return "Missing address or operator", 400
    if len(addr_text) > 500:
        return "Address too long", 400
    op = User.query.filter_by(id=op_id, role="op").first()
    if not op:
        return "Invalid operator", 400
    Address.query.filter_by(assigned_op_id=op_id).delete()
    addr = Address(
        address_text=addr_text,
        assigned_op_id=op_id,
        created_by_admin_id=session["user_id"]
    )
    db.session.add(addr)
    log_event(f"Address set for {op.username}: {addr_text}")
    db.session.commit()

    # ── Send Slack DM to operator if they have a Slack ID set ──
    if op.slack_user_id:
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "New Address Assignment",
                    "emoji": False
                }
            },
            {
                "type": "divider"
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Address:* {addr_text}"
                }
            }
        ]
        
        if booking_time:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Booking Time:* {booking_time}\n\nPlease be on time for your scheduled appointment."
                }
            })
        
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Assigned by OpsCenter at {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                }
            ]
        })
        
        send_slack_dm(
            op.slack_user_id,
            "",
            blocks=blocks
        )

    return redirect(url_for("admin"))

@app.route("/export_csv")
@admin_required
def export_csv():
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["OPERATOR", "LOG_DATE", "LOG_TIME", "START_MILES", "END_MILES", "NET_DISTANCE", "SHIFT_IN", "SHIFT_OUT", "LAT", "LNG", "NOTES"])
    for log in Log.query.order_by(Log.submitted_at).all():
        dt_parts = log.submitted_at.split(" ")
        try:
            dist = log.end_mileage - log.start_mileage
        except Exception:
            dist = "ERR"
        cw.writerow([
            log.username_snapshot, dt_parts[0], dt_parts[1],
            log.start_mileage, log.end_mileage, dist,
            log.start_shift_time, log.end_shift_time,
            log.lat or "N/A", log.lng or "N/A", log.notes
        ])
    response = make_response(si.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=OP_REPORT_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    response.headers["Content-type"] = "text/csv"
    return response

@app.route("/admin")
@admin_required
def admin():
    all_users = User.query.all()
    all_logs = Log.query.order_by(Log.submitted_at).all()
    all_breaks = Break.query.order_by(Break.timestamp.desc()).all()
    all_etas = Eta.query.order_by(Eta.timestamp.desc()).all()
    all_addresses = Address.query.order_by(Address.timestamp.desc()).all()
    events = Event.query.order_by(Event.id.desc()).limit(50).all()

    user_map = {u.id: u for u in all_users}

    grouped_logs = {}
    grouped_breaks = {}
    grouped_etas = {}
    grouped_addresses = {}
    total_miles = 0

    for log in all_logs:
        name = log.username_snapshot
        date = log.submitted_at.split(" ")[0]
        grouped_logs.setdefault(name, {}).setdefault(date, []).append(log)
        try:
            total_miles += (log.end_mileage - log.start_mileage)
        except Exception:
            pass

    for br in all_breaks:
        user = user_map.get(br.user_id)
        if not user:
            continue
        date = br.timestamp.split(" ")[0]
        grouped_breaks.setdefault(user.username, {}).setdefault(date, []).append(br)

    for et in all_etas:
        user = user_map.get(et.user_id)
        if not user:
            continue
        date = et.timestamp.split(" ")[0]
        grouped_etas.setdefault(user.username, {}).setdefault(date, []).append(et)

    for addr in all_addresses:
        user = user_map.get(addr.assigned_op_id)
        if not user:
            continue
        date = addr.timestamp.split(" ")[0]
        grouped_addresses.setdefault(user.username, {}).setdefault(date, []).append(addr)

    all_op_names = sorted(set(
        list(grouped_logs.keys()) +
        list(grouped_breaks.keys()) +
        list(grouped_etas.keys()) +
        list(grouped_addresses.keys())
    ))

    unread = get_unread_count(session["user_id"])

    return render_template_string(ADMIN_HTML,
        grouped_logs=grouped_logs,
        grouped_breaks=grouped_breaks,
        grouped_etas=grouped_etas,
        grouped_addresses=grouped_addresses,
        all_op_names=all_op_names,
        all_users=all_users,
        events=events,
        total_miles=total_miles,
        log_count=len(all_logs),
        unread_count=unread,
        vapid_public_key=VAPID_PUBLIC_KEY
    )

@app.route("/op")
@login_required
def op():
    user_logs = Log.query.filter_by(user_id=session["user_id"]).order_by(Log.submitted_at.desc()).all()
    break_records = Break.query.filter_by(user_id=session["user_id"]).order_by(Break.id.desc()).all()
    in_break = bool(break_records and break_records[0].action == "Start")
    assigned_address = Address.query.filter_by(assigned_op_id=session["user_id"]).first()
    unread = get_unread_count(session["user_id"])
    return render_template_string(OP_HTML,
        user=session["username"],
        logs=user_logs,
        break_records=break_records,
        in_break=in_break,
        assigned_address=assigned_address,
        unread_count=unread,
        vapid_public_key=VAPID_PUBLIC_KEY
    )

@app.route("/uploads/<string:filename>")
def uploaded_file(filename):
    if "/" in filename or "\\" in filename or ".." in filename:
        return "Invalid filename", 400
    return send_from_directory(os.path.abspath(UPLOAD_FOLDER), filename)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ─── MESSAGING API ───────────────────────────────────────────

@app.route("/api/channels")
@login_required
def api_channels():
    uid = session["user_id"]
    memberships = ChannelMember.query.filter_by(user_id=uid).all()
    all_users = {u.id: u.username for u in User.query.all()}
    result = []
    for m in memberships:
        ch = Channel.query.get(m.channel_id)
        if not ch:
            continue
        unread = Message.query.filter(
            Message.channel_id == ch.id,
            Message.sent_at > m.last_read_at,
            Message.sender_id != uid
        ).count()
        last_msg = Message.query.filter_by(channel_id=ch.id).order_by(Message.id.desc()).first()
        display_name = ch.name or "general"
        if ch.is_dm:
            other_members = [x for x in ch.members if x.user_id != uid]
            if other_members:
                display_name = all_users.get(other_members[0].user_id, "Unknown")
        result.append({
            "id": ch.id,
            "name": display_name,
            "is_dm": ch.is_dm,
            "unread": unread,
            "last_msg": last_msg.body[:40] if last_msg else "",
            "last_at": last_msg.sent_at if last_msg else ""
        })
    result.sort(key=lambda x: x["last_at"], reverse=True)
    return jsonify(result)


@app.route("/api/messages/<int:channel_id>")
@login_required
def api_get_messages(channel_id):
    uid = session["user_id"]
    m = ChannelMember.query.filter_by(channel_id=channel_id, user_id=uid).first()
    if not m:
        return jsonify({"error": "Not a member"}), 403
    msgs = Message.query.filter_by(channel_id=channel_id).order_by(Message.id.desc()).limit(60).all()
    msgs.reverse()
    return jsonify([{
        "id": msg.id,
        "sender": msg.sender_name,
        "body": msg.body,
        "sent_at": msg.sent_at,
        "is_alert": msg.is_alert,
        "is_mine": msg.sender_id == uid
    } for msg in msgs])


@app.route("/api/messages/<int:channel_id>", methods=["POST"])
@login_required
def api_send_message(channel_id):
    uid = session["user_id"]
    uname = session["username"]
    m = ChannelMember.query.filter_by(channel_id=channel_id, user_id=uid).first()
    if not m:
        return jsonify({"error": "Not a member"}), 403
    body = request.json.get("body", "").strip()
    if not body or len(body) > 1000:
        return jsonify({"error": "Invalid message"}), 400
    msg = Message(channel_id=channel_id, sender_id=uid, sender_name=uname, body=body)
    db.session.add(msg)
    db.session.commit()
    all_members = ChannelMember.query.filter_by(channel_id=channel_id).all()
    for mem in all_members:
        if mem.user_id == uid:
            continue
        send_push_to_user(mem.user_id, f"OpsCenter — {uname}", body[:80], "/admin" if session["role"] == "admin" else "/op")
    mentions = parse_mentions(body)
    for username in mentions:
        mentioned_user = User.query.filter(db.func.lower(User.username) == username.lower()).first()
        if mentioned_user and mentioned_user.id != uid:
            existing = ChannelMember.query.filter_by(channel_id=channel_id, user_id=mentioned_user.id).first()
            if not existing:
                db.session.add(ChannelMember(channel_id=channel_id, user_id=mentioned_user.id))
                db.session.commit()
    return jsonify({"ok": True, "id": msg.id})


@app.route("/api/channels/dm", methods=["POST"])
@login_required
def api_start_dm():
    uid = session["user_id"]
    target_name = request.json.get("username", "").strip()
    target = User.query.filter(db.func.lower(User.username) == target_name.lower()).first()
    if not target:
        return jsonify({"error": "User not found"}), 404
    if target.id == uid:
        return jsonify({"error": "Cannot DM yourself"}), 400
    ch = get_dm_channel(uid, target.id)
    return jsonify({"channel_id": ch.id, "name": target.username})


@app.route("/api/channels/alert", methods=["POST"])
@admin_required
def api_send_alert():
    uid = session["user_id"]
    body = request.json.get("body", "").strip()
    if not body or len(body) > 500:
        return jsonify({"error": "Invalid alert"}), 400
    ch = get_or_create_general_channel()
    msg = Message(channel_id=ch.id, sender_id=uid, sender_name=session["username"], body=body, is_alert=True)
    db.session.add(msg)
    db.session.commit()
    for u in User.query.all():
        if u.id != uid:
            send_push_to_user(u.id, "⚠️ ALERT — OpsCenter", body[:100], "/op")
    return jsonify({"ok": True})


@app.route("/api/channels/read/<int:channel_id>", methods=["POST"])
@login_required
def api_mark_read(channel_id):
    uid = session["user_id"]
    m = ChannelMember.query.filter_by(channel_id=channel_id, user_id=uid).first()
    if m:
        m.last_read_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/unread")
@login_required
def api_unread():
    return jsonify({"count": get_unread_count(session["user_id"])})


@app.route("/api/users")
@login_required
def api_users():
    uid = session["user_id"]
    users = [{"id": u.id, "username": u.username, "role": u.role}
             for u in User.query.all() if u.id != uid]
    return jsonify(users)


@app.route("/api/push/subscribe", methods=["POST"])
@login_required
def api_push_subscribe():
    uid = session["user_id"]
    sub_data = request.json
    if not sub_data or "endpoint" not in sub_data:
        return jsonify({"error": "Invalid subscription"}), 400
    endpoint = sub_data.get("endpoint", "")
    existing_subs = PushSubscription.query.filter_by(user_id=uid).all()
    for s in existing_subs:
        try:
            if json.loads(s.subscription_json).get("endpoint") == endpoint:
                return jsonify({"ok": True, "existing": True})
        except Exception:
            pass
    sub = PushSubscription(user_id=uid, subscription_json=json.dumps(sub_data))
    db.session.add(sub)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/push/vapid-public-key")
def api_vapid_public_key():
    return jsonify({"key": VAPID_PUBLIC_KEY})


@app.route("/sw.js")
def service_worker():
    sw_js = """
self.addEventListener('push', function(event) {
    let data = {};
    try { data = event.data.json(); } catch(e) { data = { title: 'OpsCenter', body: event.data ? event.data.text() : 'New message' }; }
    const options = {
        body: data.body || 'New message',
        icon: '/static/icon.png',
        badge: '/static/badge.png',
        vibrate: [200, 100, 200],
        data: { url: data.url || '/' },
        requireInteraction: false
    };
    event.waitUntil(self.registration.showNotification(data.title || 'OpsCenter', options));
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    const url = event.notification.data && event.notification.data.url ? event.notification.data.url : '/';
    event.waitUntil(clients.matchAll({ type: 'window' }).then(function(clientList) {
        for (let i = 0; i < clientList.length; i++) {
            if (clientList[i].url === url && 'focus' in clientList[i]) return clientList[i].focus();
        }
        if (clients.openWindow) return clients.openWindow(url);
    }));
});

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());
"""
    response = make_response(sw_js)
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Cache-Control"] = "no-cache"
    return response

# ---------------- UI COMPONENTS ----------------

COMMON_HEAD = """
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>OpsCenter v3.3 | Tactical</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;600;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #fbbf24; --bg: #0b0f1a; --card: #161e2d; }
        body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: var(--bg); color: #e2e8f0; scroll-behavior: smooth; }
        .font-mono { font-family: 'JetBrains Mono', monospace; }
        .glass-panel { background: rgba(22, 30, 45, 0.85); backdrop-filter: blur(24px); border: 1px solid rgba(255, 255, 255, 0.08); }
        .input-field { background: #0f172a; border: 1px solid #334155; color: white; border-radius: 14px; width:100%; padding: 0.85rem; transition: 0.3s; }
        .input-field:focus { border-color: var(--accent); outline: none; box-shadow: 0 0 15px rgba(251, 191, 36, 0.15); }
        .btn-main { background: var(--accent); color: #000; font-weight: 800; border-radius: 14px; transition: 0.3s cubic-bezier(0.4, 0, 0.2, 1); text-transform: uppercase; letter-spacing: 0.5px; }
        .btn-main:hover { transform: translateY(-2px); box-shadow: 0 10px 20px -10px var(--accent); opacity: 0.9; }
        .label-caps { font-size: 10px; font-weight: 800; color: #64748b; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 8px; display: block; }
        .drawer-content { position: fixed; top: 0; right: -100%; width: 100%; max-width: 450px; height: 100%; background: var(--card); transition: 0.5s cubic-bezier(0.4, 0, 0.2, 1); z-index: 100; padding: 2.5rem; border-left: 1px solid #334155; box-shadow: -20px 0 40px rgba(0,0,0,0.5); overflow-y: auto; }
        .drawer-content.active { right: 0; }
        .transmission-card { position: relative; transition: transform 0.2s ease, box-shadow 0.2s ease; }
        .transmission-card:hover { transform: translateY(-2px); box-shadow: 0 16px 48px rgba(0,0,0,0.25); }
        .card-actions { position: absolute; top: 1rem; right: 1rem; display: flex; gap: 0.4rem; opacity: 0; transform: translateY(-4px); transition: opacity 0.2s ease, transform 0.2s ease; z-index: 10; pointer-events: none; }
        .transmission-card:hover .card-actions { opacity: 1; transform: translateY(0); pointer-events: auto; }
        .card-actions form { margin: 0; }
        .act-btn { display: inline-flex; align-items: center; gap: 5px; padding: 0.45rem 0.85rem; border-radius: 8px; font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; cursor: pointer; border: none; transition: all 0.15s ease; white-space: nowrap; }
        .act-btn svg { width: 12px; height: 12px; flex-shrink: 0; }
        .act-btn-edit { background: rgba(255,255,255,0.08); color: #e2e8f0; border: 1px solid rgba(255,255,255,0.12); }
        .act-btn-edit:hover { background: rgba(255,255,255,0.15); border-color: rgba(255,255,255,0.25); }
        .act-btn-delete { background: rgba(239,68,68,0.1); color: #f87171; border: 1px solid rgba(239,68,68,0.2); }
        .act-btn-delete:hover { background: rgba(239,68,68,0.2); border-color: rgba(239,68,68,0.4); }
        .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.75); z-index: 200; display: none; align-items: center; justify-content: center; padding: 1rem; backdrop-filter: blur(6px); }
        .modal-overlay.open { display: flex; }
        .modal-box { background: #161e2d; border: 1px solid rgba(255,255,255,0.1); border-radius: 24px; padding: 2rem; width: 100%; max-width: 680px; max-height: 90vh; overflow-y: auto; box-shadow: 0 30px 80px rgba(0,0,0,0.6); }
        .modal-box h2 { font-size: 1.4rem; font-weight: 800; color: white; margin-bottom: 0.25rem; }
        .modal-box p.sub { font-size: 0.7rem; color: #64748b; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 1.5rem; }
        .modal-close { float: right; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1); color: #94a3b8; border-radius: 8px; width: 32px; height: 32px; cursor: pointer; font-size: 1.1rem; display: flex; align-items: center; justify-content: center; transition: 0.15s; }
        .modal-close:hover { background: rgba(255,255,255,0.12); color: white; }
        .photo-preview { width: 100%; height: 140px; object-fit: cover; border-radius: 12px; border: 1px solid rgba(255,255,255,0.08); margin-bottom: 0.5rem; }
        .photo-label { font-size: 0.65rem; font-weight: 800; color: #64748b; text-transform: uppercase; letter-spacing: 1.5px; display: block; margin-bottom: 6px; margin-top: 12px; }
        .sidebar-btn.active { border-color: var(--accent); background: rgba(251, 191, 36, 0.1); color: var(--accent); }
        ::-webkit-scrollbar { width: 5px; }
        ::-webkit-scrollbar-thumb { background: #334155; border-radius: 10px; }

        /* ── CHAT PANEL ── */
        #chat-panel { position: fixed; bottom: 0; right: 0; width: 100%; max-width: 420px; height: 580px; background: #0f172a; border: 1px solid #1e293b; border-bottom: none; border-radius: 20px 20px 0 0; z-index: 300; display: flex; flex-direction: column; transform: translateY(100%); transition: transform 0.4s cubic-bezier(0.4,0,0.2,1); box-shadow: 0 -20px 60px rgba(0,0,0,0.6); }
        #chat-panel.open { transform: translateY(0); }
        .chat-header { padding: 1rem 1.2rem; border-bottom: 1px solid #1e293b; display: flex; align-items: center; gap: 0.8rem; flex-shrink: 0; background: #0d1424; border-radius: 20px 20px 0 0; }
        .chat-body { display: flex; flex: 1; overflow: hidden; }
        .chat-sidebar { width: 130px; border-right: 1px solid #1e293b; overflow-y: auto; flex-shrink: 0; background: #0b0f1a; }
        .chat-messages { flex: 1; overflow-y: auto; padding: 0.8rem; display: flex; flex-direction: column; gap: 0.5rem; }
        .chat-input-area { border-top: 1px solid #1e293b; padding: 0.8rem; display: flex; gap: 0.5rem; flex-shrink: 0; background: #0d1424; }
        .chat-input-area input { background: #1e293b; border: 1px solid #334155; color: white; border-radius: 12px; padding: 0.6rem 0.8rem; font-size: 0.8rem; flex: 1; outline: none; font-family: 'Plus Jakarta Sans', sans-serif; }
        .chat-input-area input:focus { border-color: var(--accent); }
        .chat-send-btn { background: var(--accent); color: #000; border: none; border-radius: 12px; padding: 0.6rem 1rem; font-weight: 800; font-size: 0.75rem; cursor: pointer; flex-shrink: 0; }
        .channel-item { padding: 0.6rem 0.7rem; cursor: pointer; border-bottom: 1px solid #1e293b; transition: background 0.15s; }
        .channel-item:hover, .channel-item.active { background: rgba(251,191,36,0.08); }
        .channel-item .ch-name { font-size: 0.7rem; font-weight: 700; color: #e2e8f0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .channel-item .ch-badge { background: #ef4444; color: white; font-size: 0.55rem; font-weight: 800; border-radius: 999px; padding: 1px 5px; }
        .msg-bubble { max-width: 80%; padding: 0.5rem 0.75rem; border-radius: 14px; font-size: 0.78rem; line-height: 1.4; word-break: break-word; }
        .msg-bubble.mine { background: var(--accent); color: #000; align-self: flex-end; border-bottom-right-radius: 4px; }
        .msg-bubble.theirs { background: #1e293b; color: #e2e8f0; align-self: flex-start; border-bottom-left-radius: 4px; }
        .msg-bubble.alert { background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.3); color: #fca5a5; align-self: stretch; border-radius: 10px; }
        .msg-meta { font-size: 0.6rem; color: #64748b; margin-bottom: 2px; }
        .msg-mention { color: var(--accent); font-weight: 700; }
        .chat-fab { position: fixed; bottom: 1.5rem; right: 1.5rem; z-index: 299; background: var(--accent); color: #000; border: none; border-radius: 50%; width: 56px; height: 56px; cursor: pointer; box-shadow: 0 8px 24px rgba(251,191,36,0.4); display: flex; align-items: center; justify-content: center; transition: transform 0.2s; }
        .chat-fab:hover { transform: scale(1.08); }
        .chat-fab-badge { position: absolute; top: -4px; right: -4px; background: #ef4444; color: white; font-size: 0.6rem; font-weight: 800; border-radius: 999px; min-width: 18px; height: 18px; display: flex; align-items: center; justify-content: center; padding: 0 4px; border: 2px solid var(--bg); }
        .autocomplete-list { position: absolute; bottom: 100%; left: 0; right: 0; background: #1e293b; border: 1px solid #334155; border-radius: 10px; max-height: 140px; overflow-y: auto; z-index: 400; }
        .autocomplete-item { padding: 0.5rem 0.8rem; font-size: 0.78rem; cursor: pointer; color: #e2e8f0; }
        .autocomplete-item:hover { background: rgba(251,191,36,0.1); color: var(--accent); }
        .new-dm-btn { width: 100%; padding: 0.5rem 0.7rem; font-size: 0.65rem; font-weight: 800; color: #fbbf24; background: transparent; border: none; cursor: pointer; text-align: left; border-bottom: 1px solid #1e293b; text-transform: uppercase; letter-spacing: 0.05em; }
        .new-dm-btn:hover { background: rgba(251,191,36,0.05); }
        .slack-badge { display: inline-flex; align-items: center; gap: 4px; background: rgba(74,144,74,0.15); border: 1px solid rgba(74,144,74,0.3); color: #4CAF50; border-radius: 6px; padding: 2px 7px; font-size: 0.6rem; font-weight: 800; text-transform: uppercase; letter-spacing: 0.05em; }
    </style>
</head>
"""

NAV_BAR = """
<nav class="sticky top-0 z-50 glass-panel border-b border-white/5 mb-8">
    <div class="max-w-7xl mx-auto px-6 h-20 flex justify-between items-center">
        <div class="flex items-center gap-4">
            <div class="w-10 h-10 bg-amber-400 rounded-xl flex items-center justify-center text-black font-black italic shadow-lg shadow-amber-400/20">O</div>
            <div class="leading-none">
                <span class="text-2xl font-black italic tracking-tighter uppercase text-white block">Ops<span class="text-amber-400">Center</span></span>
                <span class="text-[9px] font-bold text-gray-500 uppercase tracking-[3px]">Mission-Protocol-v3.3</span>
            </div>
        </div>
        <div class="flex items-center gap-4">
            <div class="hidden md:flex flex-col items-end leading-none">
                <span class="text-[10px] font-bold text-gray-500 uppercase mb-1">Authenticated</span>
                <span class="text-sm font-bold text-white">{{ session['username'] }}</span>
            </div>
            <button onclick="toggleDrawer()" class="w-12 h-12 flex items-center justify-center hover:bg-white/5 rounded-2xl transition border border-white/5">
                <svg class="w-6 h-6 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16m-7 6h7"></path></svg>
            </button>
        </div>
    </div>
</nav>
"""

DRAWER_HTML = """
<div id="overlay" class="fixed inset-0 bg-black/90 z-[90] hidden backdrop-blur-sm" onclick="toggleDrawer()"></div>
<div id="drawer" class="drawer-content">
    <div class="flex justify-between items-center mb-10">
        <div>
            <h2 class="text-2xl font-black text-amber-400 italic leading-none">SYSTEM</h2>
            <p class="text-xs font-bold text-gray-500 uppercase mt-1">Configuration & Personnel</p>
        </div>
        <button onclick="toggleDrawer()" class="w-10 h-10 flex items-center justify-center text-3xl text-gray-400 hover:text-white transition">x</button>
    </div>
    <div class="space-y-10">
        {% if session['role'] == 'admin' %}
        <section>
            <p class="label-caps mb-4">Personnel Intake</p>
            <form action="/create_user" method="POST" class="space-y-3 bg-black/40 p-5 rounded-3xl border border-white/5">
                <input name="username" placeholder="Operator Name" class="input-field" required>
                <input name="password" type="password" placeholder="Password" class="input-field" required>
                <button class="btn-main w-full py-4 text-xs font-black">Provision Account</button>
            </form>
        </section>
        <section>
            <p class="label-caps mb-4">Address Assignment</p>
            <form action="/set_address" method="POST" class="space-y-3 bg-black/40 p-5 rounded-3xl border border-white/5">
                <textarea name="address_text" placeholder="Enter address details..." class="input-field" rows="3" required></textarea>
                <input type="time" name="booking_time" class="input-field" placeholder="Booking Time">
                <select name="op_id" class="input-field" required>
                    <option value="">Select Operator</option>
                    {% for u in all_users %}
                    {% if u.role == 'op' %}
                    <option value="{{ u.id }}">{{ u.username }}</option>
                    {% endif %}
                    {% endfor %}
                </select>
                <button class="btn-main w-full py-4 text-xs font-black">Assign Address</button>
            </form>
        </section>
        <section>
            <p class="label-caps mb-4">Active Directory</p>
            <div class="space-y-4">
                {% for u in all_users %}
                <div class="bg-black/30 p-5 rounded-2xl border border-white/5 hover:border-amber-400/30 transition">
                    <div class="flex justify-between items-start mb-4">
                        <div>
                            <h4 class="font-bold text-white text-lg leading-none">{{ u.username }}</h4>
                            <p class="text-[10px] text-gray-500 font-mono mt-1 uppercase">Role: {{ u.role }} | Last: {{ u.last_login }}</p>
                            {% if u.slack_user_id %}
                            <span class="slack-badge mt-2 inline-flex">
                                <svg width="8" height="8" viewBox="0 0 24 24" fill="currentColor"><path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/></svg>
                                Slack Connected
                            </span>
                            {% endif %}
                        </div>
                        {% if u.username != session['username'] %}
                        <form action="/delete_user/{{ u.username }}" method="POST" onsubmit="return confirm('Permanently purge this operator?');">
                            <button class="w-8 h-8 flex items-center justify-center rounded-lg text-red-500 hover:bg-red-500/10 transition">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
                            </button>
                        </form>
                        {% endif %}
                    </div>
                    <form action="/update_user" method="POST" class="space-y-2">
                        <input type="hidden" name="target_username" value="{{ u.username }}">
                        <input name="new_username" placeholder="New Name" class="input-field !text-[11px] !py-2">
                        <input name="new_password" type="password" placeholder="New Password" class="input-field !text-[11px] !py-2">
                        <input name="slack_user_id" placeholder="Slack User ID (e.g. U012AB3CD)" class="input-field !text-[11px] !py-2" value="{{ u.slack_user_id or '' }}">
                        <button class="bg-white/10 hover:bg-amber-400 hover:text-black w-full py-2 text-[10px] font-black rounded-xl uppercase transition">Update</button>
                    </form>
                </div>
                {% endfor %}
            </div>
        </section>
        {% endif %}
        <div class="pt-6 border-t border-white/5">
            <a href="/logout" class="block w-full text-center py-5 rounded-2xl bg-red-600/10 text-red-500 font-black uppercase text-xs border border-red-500/20 hover:bg-red-600 hover:text-white transition">Sign Out & Lock System</a>
        </div>
    </div>
</div>
<script>
function toggleDrawer(){
    document.getElementById('drawer').classList.toggle('active');
    document.getElementById('overlay').classList.toggle('hidden');
}
</script>
"""

CHAT_HTML = """
<!-- ── CHAT FAB ── -->
<button class="chat-fab" onclick="toggleChat()" id="chat-fab-btn" title="Team Comms">
    <svg width="24" height="24" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"/></svg>
    <span class="chat-fab-badge" id="fab-badge" style="display:none">0</span>
</button>

<!-- ── CHAT PANEL ── -->
<div id="chat-panel">
    <div class="chat-header">
        <button onclick="toggleChat()" style="background:none;border:none;color:#64748b;cursor:pointer;font-size:1.2rem;line-height:1;padding:0;margin-right:4px">✕</button>
        <div style="flex:1">
            <p style="font-size:0.7rem;font-weight:800;color:#fbbf24;text-transform:uppercase;letter-spacing:2px;margin:0">OpsCenter Comms</p>
            <p id="chat-channel-title" style="font-size:0.85rem;font-weight:700;color:#e2e8f0;margin:0">#general</p>
        </div>
        {% if session['role'] == 'admin' %}
        <button onclick="sendAlert()" title="Broadcast Alert" style="background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.3);color:#f87171;border-radius:8px;padding:0.3rem 0.6rem;font-size:0.65rem;font-weight:800;cursor:pointer;text-transform:uppercase">⚠ Alert</button>
        {% endif %}
    </div>
    <div class="chat-body">
        <div class="chat-sidebar" id="chat-sidebar">
            <div style="padding:0.5rem 0.7rem;font-size:0.6rem;font-weight:800;color:#64748b;text-transform:uppercase;letter-spacing:1px;border-bottom:1px solid #1e293b">Channels</div>
            <div id="channel-list"></div>
            <button class="new-dm-btn" onclick="startDM()">+ New DM</button>
        </div>
        <div class="chat-messages" id="chat-messages">
            <div style="text-align:center;color:#334155;font-size:0.7rem;margin:auto">Loading...</div>
        </div>
    </div>
    <div class="chat-input-area" style="position:relative">
        <div id="mention-autocomplete" class="autocomplete-list" style="display:none"></div>
        <input type="text" id="chat-input" placeholder="Message... use @name to tag" maxlength="1000"
               oninput="handleMentionInput(this)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMessage()}">
        <button class="chat-send-btn" onclick="sendMessage()">Send</button>
    </div>
</div>

<script>
const CURRENT_USER = "{{ session['username'] }}";
const IS_ADMIN = {{ 'true' if session['role'] == 'admin' else 'false' }};
const VAPID_PUBLIC_KEY = "{{ vapid_public_key }}";

let currentChannelId = null;
let allUsers = [];
let chatOpen = false;
let pollInterval = null;
let lastMsgId = 0;

async function registerPushNotifications() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
    if (!VAPID_PUBLIC_KEY) return;
    try {
        const reg = await navigator.serviceWorker.register('/sw.js');
        const existing = await reg.pushManager.getSubscription();
        if (existing) { await savePushSub(existing); return; }
        const permission = await Notification.requestPermission();
        if (permission !== 'granted') return;
        const sub = await reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY)
        });
        await savePushSub(sub);
    } catch(e) { console.log('Push setup skipped:', e.message); }
}

function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = atob(base64);
    return Uint8Array.from([...rawData].map(c => c.charCodeAt(0)));
}

async function savePushSub(sub) {
    await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(sub.toJSON())
    });
}

function playPing() {
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.connect(g); g.connect(ctx.destination);
        o.type = 'sine'; o.frequency.value = 880;
        g.gain.setValueAtTime(0, ctx.currentTime);
        g.gain.linearRampToValueAtTime(0.3, ctx.currentTime + 0.01);
        g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
        o.start(ctx.currentTime); o.stop(ctx.currentTime + 0.4);
    } catch(e){}
}

function toggleChat() {
    chatOpen = !chatOpen;
    document.getElementById('chat-panel').classList.toggle('open', chatOpen);
    if (chatOpen) {
        loadChannels();
        if (!pollInterval) pollInterval = setInterval(pollMessages, 8000);
        registerPushNotifications();
    } else {
        clearInterval(pollInterval); pollInterval = null;
    }
}

async function loadChannels() {
    const res = await fetch('/api/channels');
    const channels = await res.json();
    allUsers = (await (await fetch('/api/users')).json());
    const list = document.getElementById('channel-list');
    list.innerHTML = '';
    channels.forEach(ch => {
        const div = document.createElement('div');
        div.className = 'channel-item' + (ch.id === currentChannelId ? ' active' : '');
        div.dataset.id = ch.id;
        div.onclick = () => openChannel(ch.id, ch.name);
        div.innerHTML = `<div style="display:flex;align-items:center;justify-content:space-between">
            <span class="ch-name">${ch.is_dm ? '💬 ' : '#'}${ch.name}</span>
            ${ch.unread > 0 ? `<span class="ch-badge">${ch.unread}</span>` : ''}
        </div>
        <div style="font-size:0.6rem;color:#475569;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${ch.last_msg || ''}</div>`;
        list.appendChild(div);
    });
    if (!currentChannelId && channels.length > 0) {
        openChannel(channels[0].id, channels[0].name);
    }
    const total = channels.reduce((s, c) => s + c.unread, 0);
    updateFabBadge(total);
}

async function openChannel(channelId, name) {
    currentChannelId = channelId;
    document.getElementById('chat-channel-title').textContent = (name === 'general' ? '#' : '') + name;
    document.querySelectorAll('.channel-item').forEach(el => el.classList.toggle('active', parseInt(el.dataset.id) === channelId));
    await loadMessages();
    await fetch(`/api/channels/read/${channelId}`, {method:'POST'});
    await loadChannels();
}

async function loadMessages() {
    if (!currentChannelId) return;
    const res = await fetch(`/api/messages/${currentChannelId}`);
    const msgs = await res.json();
    renderMessages(msgs);
    if (msgs.length) lastMsgId = msgs[msgs.length - 1].id;
}

function renderMessages(msgs) {
    const container = document.getElementById('chat-messages');
    const wasAtBottom = container.scrollHeight - container.clientHeight <= container.scrollTop + 40;
    container.innerHTML = '';
    msgs.forEach(msg => {
        const wrapper = document.createElement('div');
        wrapper.style.cssText = `display:flex;flex-direction:column;align-items:${msg.is_mine ? 'flex-end' : 'flex-start'}`;
        if (!msg.is_mine && !msg.is_alert) {
            const meta = document.createElement('div');
            meta.className = 'msg-meta';
            meta.style.marginLeft = '4px';
            meta.textContent = msg.sender + ' · ' + msg.sent_at.slice(11,16);
            wrapper.appendChild(meta);
        }
        const bubble = document.createElement('div');
        bubble.className = 'msg-bubble ' + (msg.is_alert ? 'alert' : msg.is_mine ? 'mine' : 'theirs');
        if (msg.is_alert) {
            bubble.innerHTML = '⚠️ <strong>ADMIN ALERT</strong><br>' + formatBody(msg.body);
        } else {
            bubble.innerHTML = formatBody(msg.body);
        }
        if (msg.is_mine) {
            const meta = document.createElement('div');
            meta.className = 'msg-meta';
            meta.style.marginRight = '4px';
            meta.textContent = msg.sent_at.slice(11,16);
            wrapper.appendChild(bubble);
            wrapper.appendChild(meta);
        } else {
            wrapper.appendChild(bubble);
        }
        container.appendChild(wrapper);
    });
    if (wasAtBottom) container.scrollTop = container.scrollHeight;
}

function formatBody(text) {
    return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
               .replace(/@(\w+)/g, '<span class="msg-mention">@$1</span>');
}

async function sendMessage() {
    const input = document.getElementById('chat-input');
    const body = input.value.trim();
    if (!body || !currentChannelId) return;
    input.value = '';
    hideMentionAutocomplete();
    await fetch(`/api/messages/${currentChannelId}`, {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({body})
    });
    await loadMessages();
}

async function pollMessages() {
    if (!currentChannelId) return;
    const res = await fetch(`/api/messages/${currentChannelId}`);
    const msgs = await res.json();
    if (msgs.length && msgs[msgs.length - 1].id !== lastMsgId) {
        const newOnes = msgs.filter(m => m.id > lastMsgId && !m.is_mine);
        if (newOnes.length > 0) {
            playPing();
            newOnes.forEach(m => showToast(`${m.sender}: ${m.body.slice(0,60)}`));
        }
        renderMessages(msgs);
        lastMsgId = msgs[msgs.length - 1].id;
        if (chatOpen) await fetch(`/api/channels/read/${currentChannelId}`, {method:'POST'});
    }
    const uRes = await fetch('/api/unread');
    const {count} = await uRes.json();
    updateFabBadge(count);
}

setInterval(async () => {
    const uRes = await fetch('/api/unread');
    const {count} = await uRes.json();
    updateFabBadge(count);
}, 20000);

function updateFabBadge(count) {
    const badge = document.getElementById('fab-badge');
    if (count > 0) { badge.style.display = 'flex'; badge.textContent = count > 99 ? '99+' : count; }
    else { badge.style.display = 'none'; }
}

function showToast(msg) {
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed;top:1rem;left:50%;transform:translateX(-50%);background:#1e293b;border:1px solid #334155;color:#e2e8f0;padding:0.7rem 1.2rem;border-radius:12px;font-size:0.78rem;font-weight:600;z-index:9999;box-shadow:0 8px 24px rgba(0,0,0,0.4);max-width:90vw;text-align:center';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
}

async function startDM() {
    const username = prompt('Enter operator username to DM:');
    if (!username) return;
    const res = await fetch('/api/channels/dm', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({username})
    });
    if (!res.ok) { showToast('User not found'); return; }
    const {channel_id, name} = await res.json();
    await loadChannels();
    openChannel(channel_id, name);
}

async function sendAlert() {
    const body = prompt('Enter alert message to broadcast to all operators:');
    if (!body) return;
    const res = await fetch('/api/channels/alert', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({body})
    });
    if (res.ok) { showToast('Alert sent to all operators'); await loadMessages(); }
}

function handleMentionInput(input) {
    const val = input.value;
    const match = val.match(/@(\w*)$/);
    if (!match) { hideMentionAutocomplete(); return; }
    const query = match[1].toLowerCase();
    const filtered = allUsers.filter(u => u.username.toLowerCase().startsWith(query));
    if (!filtered.length) { hideMentionAutocomplete(); return; }
    const ac = document.getElementById('mention-autocomplete');
    ac.innerHTML = '';
    filtered.slice(0, 5).forEach(u => {
        const item = document.createElement('div');
        item.className = 'autocomplete-item';
        item.textContent = '@' + u.username;
        item.onmousedown = (e) => {
            e.preventDefault();
            input.value = val.replace(/@\w*$/, '@' + u.username + ' ');
            hideMentionAutocomplete();
            input.focus();
        };
        ac.appendChild(item);
    });
    ac.style.display = 'block';
}

function hideMentionAutocomplete() {
    document.getElementById('mention-autocomplete').style.display = 'none';
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('.chat-input-area')) hideMentionAutocomplete();
});

window.addEventListener('load', () => {
    if (VAPID_PUBLIC_KEY) registerPushNotifications();
    fetch('/api/unread').then(r=>r.json()).then(({count}) => updateFabBadge(count));
});
</script>
"""

EDIT_MODALS_HTML = """
<!-- Edit Log Modal -->
<div id="modal-log" class="modal-overlay">
  <div class="modal-box">
    <div class="flex justify-between items-start mb-1">
      <h2>Edit Log</h2>
      <button class="modal-close" onclick="closeModal('modal-log')">&times;</button>
    </div>
    <p class="sub">Correct the telemetry record</p>
    <form id="edit-log-form" method="POST" enctype="multipart/form-data" class="space-y-4">
      <div class="grid grid-cols-2 gap-4">
        <div><label class="label-caps">Start Mileage</label><input id="el-start" name="start_mileage" type="number" class="input-field" required></div>
        <div><label class="label-caps">End Mileage</label><input id="el-end" name="end_mileage" type="number" class="input-field" required></div>
      </div>
      <div class="grid grid-cols-2 gap-4">
        <div><label class="label-caps">Shift In</label><input id="el-sin" name="start_shift_time" type="time" class="input-field"></div>
        <div><label class="label-caps">Shift Out</label><input id="el-sout" name="end_shift_time" type="time" class="input-field"></div>
      </div>
      <div><label class="label-caps">Notes</label><textarea id="el-notes" name="notes" rows="3" class="input-field"></textarea></div>
      <div class="grid grid-cols-2 gap-4">
        <div><label class="label-caps">Lat</label><input id="el-lat" name="lat" type="text" class="input-field"></div>
        <div><label class="label-caps">Lng</label><input id="el-lng" name="lng" type="text" class="input-field"></div>
      </div>
      <div class="grid grid-cols-2 gap-4">
        <div>
          <span class="photo-label">Start Odo Photo</span>
          <img id="el-p-start" src="" class="photo-preview">
          <input name="start_photo" type="file" accept="image/*" class="text-xs text-gray-500 mt-1 block w-full">
        </div>
        <div>
          <span class="photo-label">End Odo Photo</span>
          <img id="el-p-end" src="" class="photo-preview">
          <input name="end_mileage_photo" type="file" accept="image/*" class="text-xs text-gray-500 mt-1 block w-full">
        </div>
        <div>
          <span class="photo-label">Shift In Photo</span>
          <img id="el-p-sin" src="" class="photo-preview">
          <input name="start_shift_photo" type="file" accept="image/*" class="text-xs text-gray-500 mt-1 block w-full">
        </div>
        <div>
          <span class="photo-label">Shift Out Photo</span>
          <img id="el-p-sout" src="" class="photo-preview">
          <input name="end_shift_photo" type="file" accept="image/*" class="text-xs text-gray-500 mt-1 block w-full">
        </div>
        <div class="col-span-2">
          <span class="photo-label">ETA Photo</span>
          <img id="el-p-eta" src="" class="photo-preview">
          <input name="eta_img" type="file" accept="image/*" class="text-xs text-gray-500 mt-1 block w-full">
        </div>
      </div>
      <p class="text-[10px] text-gray-500">Leave any photo blank to keep the existing one.</p>
      <button type="submit" class="btn-main w-full py-4 text-sm mt-2">Save Changes</button>
    </form>
  </div>
</div>

<!-- Edit Break Modal -->
<div id="modal-break" class="modal-overlay">
  <div class="modal-box">
    <div class="flex justify-between items-start mb-1">
      <h2>Edit Break</h2>
      <button class="modal-close" onclick="closeModal('modal-break')">&times;</button>
    </div>
    <p class="sub">Fix timestamp, location or photo</p>
    <form id="edit-break-form" method="POST" enctype="multipart/form-data" class="space-y-4">
      <div class="grid grid-cols-2 gap-4">
        <div><label class="label-caps">Action</label><input id="eb-action" name="action" type="text" class="input-field" readonly></div>
        <div><label class="label-caps">Timestamp</label><input id="eb-ts" name="timestamp" type="datetime-local" class="input-field" required></div>
      </div>
      <div class="grid grid-cols-2 gap-4">
        <div><label class="label-caps">Lat</label><input id="eb-lat" name="lat" type="text" class="input-field"></div>
        <div><label class="label-caps">Lng</label><input id="eb-lng" name="lng" type="text" class="input-field"></div>
      </div>
      <div>
        <span class="photo-label">Photo</span>
        <img id="eb-photo" src="" class="photo-preview">
        <input name="photo" type="file" accept="image/*" class="text-xs text-gray-500 mt-1 block w-full">
      </div>
      <p class="text-[10px] text-gray-500">Leave photo blank to keep the existing one.</p>
      <button type="submit" class="btn-main w-full py-4 text-sm mt-2">Save Changes</button>
    </form>
  </div>
</div>

<!-- Edit ETA Modal -->
<div id="modal-eta" class="modal-overlay">
  <div class="modal-box">
    <div class="flex justify-between items-start mb-1">
      <h2>Edit ETA</h2>
      <button class="modal-close" onclick="closeModal('modal-eta')">&times;</button>
    </div>
    <p class="sub">Fix timestamp, location or photo</p>
    <form id="edit-eta-form" method="POST" enctype="multipart/form-data" class="space-y-4">
      <div><label class="label-caps">Timestamp</label><input id="ee-ts" name="timestamp" type="datetime-local" class="input-field" required></div>
      <div class="grid grid-cols-2 gap-4">
        <div><label class="label-caps">Lat</label><input id="ee-lat" name="lat" type="text" class="input-field"></div>
        <div><label class="label-caps">Lng</label><input id="ee-lng" name="lng" type="text" class="input-field"></div>
      </div>
      <div>
        <span class="photo-label">Photo</span>
        <img id="ee-photo" src="" class="photo-preview">
        <input name="photo" type="file" accept="image/*" class="text-xs text-gray-500 mt-1 block w-full">
      </div>
      <p class="text-[10px] text-gray-500">Leave photo blank to keep the existing one.</p>
      <button type="submit" class="btn-main w-full py-4 text-sm mt-2">Save Changes</button>
    </form>
  </div>
</div>

<script>
function closeModal(id) {
    document.getElementById(id).classList.remove('open');
}
document.querySelectorAll('.modal-overlay').forEach(el => {
    el.addEventListener('click', function(e) {
        if (e.target === this) closeModal(this.id);
    });
});

function openEditLog(logId, startMi, endMi, sinT, soutT, notes, lat, lng, pStart, pEnd, pSin, pSout, pEta) {
    const form = document.getElementById('edit-log-form');
    form.action = '/edit_log/' + logId;
    document.getElementById('el-start').value = startMi || '';
    document.getElementById('el-end').value = endMi || '';
    document.getElementById('el-sin').value = sinT || '';
    document.getElementById('el-sout').value = soutT || '';
    document.getElementById('el-notes').value = notes || '';
    document.getElementById('el-lat').value = lat || '';
    document.getElementById('el-lng').value = lng || '';
    document.getElementById('el-p-start').src = pStart;
    document.getElementById('el-p-end').src = pEnd;
    document.getElementById('el-p-sin').src = pSin;
    document.getElementById('el-p-sout').src = pSout;
    document.getElementById('el-p-eta').src = pEta;
    document.getElementById('modal-log').classList.add('open');
}

function openEditBreak(id, action, timestamp, lat, lng, photo) {
    document.getElementById('edit-break-form').action = '/edit_break/' + id;
    document.getElementById('eb-action').value = action;
    document.getElementById('eb-ts').value = timestamp;
    document.getElementById('eb-lat').value = lat;
    document.getElementById('eb-lng').value = lng;
    document.getElementById('eb-photo').src = photo;
    document.getElementById('modal-break').classList.add('open');
}

function openEditEta(id, timestamp, lat, lng, photo) {
    document.getElementById('edit-eta-form').action = '/edit_eta/' + id;
    document.getElementById('ee-ts').value = timestamp;
    document.getElementById('ee-lat').value = lat;
    document.getElementById('ee-lng').value = lng;
    document.getElementById('ee-photo').src = photo;
    document.getElementById('modal-eta').classList.add('open');
}
</script>
"""

LOGIN_HTML = f"<html>{COMMON_HEAD}<body class='flex items-center justify-center min-h-screen p-6 overflow-hidden'><div class='absolute inset-0 bg-[url(\"https://www.transparenttextures.com/patterns/carbon-fibre.png\")] opacity-20'></div><div class='glass-panel p-12 rounded-[40px] w-full max-w-[440px] relative z-10 border border-white/10 shadow-2xl'><div class='text-center mb-12'><div class='w-20 h-20 bg-amber-400 rounded-3xl flex items-center justify-center text-black font-black text-4xl italic mx-auto mb-6 shadow-2xl shadow-amber-400/40'>O</div><h1 class='text-4xl font-black text-white italic uppercase tracking-tighter'>Mission<span class='text-amber-400'>Control</span></h1><p class='text-gray-500 text-xs font-bold uppercase tracking-[4px] mt-4'>System Authorization Required</p></div><form action='/login' method='POST' class='space-y-5'><div class='space-y-1'><label class='label-caps ml-2'>Identity</label><input name='username' placeholder='Operator ID' class='input-field py-4' required autofocus></div><div class='space-y-1'><label class='label-caps ml-2'>Access Code</label><input name='password' type='password' placeholder='........' class='input-field py-4' required></div><button class='btn-main w-full py-5 text-xl shadow-2xl shadow-amber-400/20 mt-6'>Authenticate</button></form></div></body></html>"

ADMIN_HTML = f"<html>{COMMON_HEAD}<body>" + NAV_BAR + DRAWER_HTML + """
<main class="max-w-7xl mx-auto px-6 pb-24">
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-12">
        <div class="glass-panel p-8 rounded-[2rem] relative overflow-hidden group">
            <div class="absolute top-0 right-0 p-4 opacity-10 group-hover:opacity-20 transition"><svg class="w-16 h-16" fill="currentColor" viewBox="0 0 20 20"><path d="M13 6a3 3 0 11-6 0 3 3 0 016 0zM18 8a2 2 0 11-4 0 2 2 0 014 0zM14 15a4 4 0 00-8 0v3h8v-3zM6 8a2 2 0 11-4 0 2 2 0 014 0zM16 18v-3a5.972 5.972 0 00-.75-2.906A3.005 3.005 0 0119 15v3h-3zM4.75 12.094A5.973 5.973 0 004 15v3H1v-3a3 3 0 013.75-2.906z"></path></svg></div>
            <p class="label-caps text-amber-400">Total Operators</p>
            <h3 class="text-5xl font-black text-white italic mt-2">{{ all_users|length }}</h3>
        </div>
        <div class="glass-panel p-8 rounded-[2rem] relative overflow-hidden group">
            <div class="absolute top-0 right-0 p-4 opacity-10 group-hover:opacity-20 transition"><svg class="w-16 h-16" fill="currentColor" viewBox="0 0 20 20"><path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z"></path></svg></div>
            <p class="label-caps text-amber-400">System Mileage</p>
            <h3 class="text-5xl font-black text-white italic mt-2">{{ total_miles }}<span class="text-xl ml-2">MI</span></h3>
        </div>
        <div class="glass-panel p-8 rounded-[2rem] relative overflow-hidden group">
            <div class="absolute top-0 right-0 p-4 opacity-10 group-hover:opacity-20 transition"><svg class="w-16 h-16" fill="currentColor" viewBox="0 0 20 20"><path d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z"></path></svg></div>
            <p class="label-caps text-amber-400">Reports Filed</p>
            <h3 class="text-5xl font-black text-white italic mt-2">{{ log_count }}</h3>
        </div>
    </div>

    <div class="flex flex-col lg:flex-row gap-10">
        <div class="w-full lg:w-80 space-y-6">
            <div class="flex items-center justify-between px-2">
                <p class="label-caps">Log Explorer</p>
                <a href="/export_csv" class="text-[10px] font-black text-amber-400 hover:underline">EXTRACT CSV</a>
            </div>
            <input type="text" id="userSearch" onkeyup="filterSidebar()" placeholder="Search Identity..." class="input-field !py-3 !text-xs font-bold border-white/10">
            <div id="sidebarList" class="space-y-2 max-h-[600px] overflow-y-auto pr-2">
                {% for op_name in all_op_names %}
                <button onclick="showUserLogs(this, '{{ op_name }}')" class="sidebar-btn w-full text-left p-4 rounded-2xl hover:bg-white/5 transition flex items-center justify-between border border-transparent group">
                    <div class="flex items-center gap-4">
                        <div class="w-10 h-10 bg-amber-400/10 rounded-xl flex items-center justify-center text-amber-400 font-black text-sm">{{ op_name[0]|upper }}</div>
                        <span class="text-sm font-bold truncate">{{ op_name }}</span>
                    </div>
                    <svg class="w-4 h-4 opacity-0 group-hover:opacity-100 transition" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7" stroke-width="3"></path></svg>
                </button>
                {% endfor %}
            </div>
            <div class="pt-8">
                <p class="label-caps px-2 mb-4">System Events</p>
                <div class="bg-black/30 rounded-2xl p-4 border border-white/5 h-64 overflow-y-auto space-y-3">
                    {% for event in events %}
                    <div class="border-l-2 border-amber-400/30 pl-3">
                        <p class="text-[10px] text-gray-500 font-mono">{{ event.time }}</p>
                        <p class="text-[11px] text-gray-300 font-bold leading-tight">{{ event.event }}</p>
                    </div>
                    {% endfor %}
                </div>
            </div>
        </div>

        <div class="flex-grow">
            {% for op_name in all_op_names %}
            {% set dates = grouped_logs.get(op_name, {}) %}
            <div id="panel-{{ op_name }}" class="user-panel hidden">
                <div class="mb-8 flex items-end justify-between border-b border-white/10 pb-6">
                    <div>
                        <h2 class="text-5xl font-black text-white italic uppercase tracking-tighter">{{ op_name }}</h2>
                        <p class="text-gray-500 font-bold text-xs uppercase tracking-widest mt-2">Historical Records Archive</p>
                    </div>
                </div>
                {% set all_dates = (dates.keys() | list) + (grouped_breaks.get(op_name, {}).keys() | list) + (grouped_etas.get(op_name, {}).keys() | list) + (grouped_addresses.get(op_name, {}).keys() | list) %}
                {% set unique_dates = [] %}
                {% for d in all_dates %}{% if d not in unique_dates %}{% set _ = unique_dates.append(d) %}{% endif %}{% endfor %}
                {% for date in unique_dates | sort(reverse=true) %}
                {% set logs = dates.get(date, []) %}
                <div class="mb-6 bg-white/5 rounded-[2rem] border border-white/5 overflow-hidden shadow-xl">
                    <button onclick="this.nextElementSibling.classList.toggle('hidden')" class="w-full p-6 flex justify-between items-center hover:bg-white/10 transition group">
                        <div class="flex items-center gap-4">
                            <div class="w-10 h-10 bg-white/5 rounded-full flex items-center justify-center"><svg class="w-5 h-5 text-gray-500" fill="currentColor" viewBox="0 0 20 20"><path d="M6 2a1 1 0 00-1 1v1H4a2 2 0 00-2 2v10a2 2 0 002 2h12a2 2 0 002-2V6a2 2 0 00-2-2h-1V3a1 1 0 10-2 0v1H7V3a1 1 0 00-1-1zm0 5a1 1 0 000 2h8a1 1 0 100-2H6z"></path></svg></div>
                            <span class="text-xl font-black text-white italic">{{ date }}</span>
                        </div>
                        <div class="flex items-center gap-4">
                            {% set break_items = grouped_breaks.get(op_name, {}).get(date, []) %}
                            {% set eta_items = grouped_etas.get(op_name, {}).get(date, []) %}
                            {% set address_items = grouped_addresses.get(op_name, {}).get(date, []) %}
                            <span class="bg-amber-400 text-black text-[10px] font-black px-3 py-1 rounded-full uppercase">{{ logs|length + break_items|length + eta_items|length + address_items|length }} TRANSMISSIONS</span>
                            <svg class="w-5 h-5 text-gray-600 group-hover:text-amber-400 transition" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M19 9l-7 7-7-7" stroke-width="3"></path></svg>
                        </div>
                    </button>
                    <div class="hidden p-8 bg-black/40 border-t border-white/5 space-y-6">

                        {% for log in logs %}
                        {% set ts_local = log.submitted_at.replace(' ', 'T')[:16] %}
                        <div class="bg-[#1c2537] rounded-3xl p-8 border border-white/10 shadow-2xl transmission-card">
                            <div class="card-actions">
                                <button type="button" class="act-btn act-btn-edit"
                                  onclick="openEditLog({{ log.id }}, '{{ log.start_mileage }}', '{{ log.end_mileage }}', '{{ log.start_shift_time or '' }}', '{{ log.end_shift_time or '' }}', '{{ log.notes or '' }}', '{{ log.lat or '' }}', '{{ log.lng or '' }}', '{{ resolve_image_url(log.start_photo) }}', '{{ resolve_image_url(log.end_mileage_photo) }}', '{{ resolve_image_url(log.start_shift_photo) }}', '{{ resolve_image_url(log.end_shift_photo) }}', '{{ resolve_image_url(log.eta_img) }}')">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>
                                    Edit
                                </button>
                                <form action="/delete_log/{{ log.id }}" method="POST" onsubmit="return confirm('Delete this log record?');">
                                    <button type="submit" class="act-btn act-btn-delete">
                                        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                                        Delete
                                    </button>
                                </form>
                            </div>
                            <div class="flex items-center gap-3 mb-6">
                                <div class="w-8 h-8 bg-amber-400/10 rounded-lg flex items-center justify-center">
                                    <svg class="w-4 h-4 text-amber-400" fill="currentColor" viewBox="0 0 20 20"><path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z"/></svg>
                                </div>
                                <div>
                                    <p class="label-caps !mb-0 text-amber-400">Daily Telemetry</p>
                                    <p class="text-[10px] text-gray-500 font-mono">{{ log.submitted_at }}</p>
                                </div>
                                {% if log.lat %}
                                <p class="ml-auto text-[9px] text-amber-400/40 font-mono">{{ log.lat }}, {{ log.lng }}</p>
                                {% endif %}
                            </div>
                            <div class="flex flex-wrap gap-8 mb-6 pb-6 border-b border-white/5">
                                <div>
                                    <p class="label-caps">Odometer Start</p>
                                    <p class="text-3xl font-mono font-bold text-white tracking-tighter">{{ log.start_mileage }}</p>
                                </div>
                                <div>
                                    <p class="label-caps">Odometer End</p>
                                    <p class="text-3xl font-mono font-bold text-white tracking-tighter">{{ log.end_mileage }}</p>
                                </div>
                                <div class="border-l border-white/10 pl-8">
                                    <p class="label-caps text-amber-400">Net Distance</p>
                                    <p class="text-4xl font-black text-amber-400 italic tracking-tighter">{{ log.end_mileage - log.start_mileage }} <span class="text-sm">MI</span></p>
                                </div>
                            </div>
                            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                                <div class="bg-black/40 p-4 rounded-2xl border border-white/5">
                                    <p class="label-caps !mb-1">Shift In</p>
                                    <p class="text-lg font-black text-white italic">{{ log.start_shift_time }}</p>
                                </div>
                                <div class="bg-black/40 p-4 rounded-2xl border border-white/5">
                                    <p class="label-caps !mb-1">Shift Out</p>
                                    <p class="text-lg font-black text-white italic">{{ log.end_shift_time }}</p>
                                </div>
                                <div class="bg-black/40 p-4 rounded-2xl border border-white/5">
                                    <p class="label-caps !mb-1">Notes</p>
                                    <p class="text-sm font-bold text-gray-300 leading-snug">{{ log.notes }}</p>
                                </div>
                            </div>
                            <p class="label-caps mb-3">Visuals <span class="text-gray-600">(click to open)</span></p>
                            <div class="flex gap-3 overflow-x-auto py-1">
                                {% set labels = ['Start Odo', 'Shift In', 'Shift Out', 'End Odo', 'ETA Proof'] %}
                                {% for img in [log.start_photo, log.start_shift_photo, log.end_shift_photo, log.end_mileage_photo, log.eta_img] %}
                                <div class="relative group flex-shrink-0">
                                    <img src="{{ resolve_image_url(img) }}" class="h-28 w-28 object-cover rounded-2xl border-2 border-white/5 group-hover:border-amber-400 transition cursor-pointer shadow-lg" onclick="window.open(this.src)">
                                    <span class="absolute bottom-1 left-1 right-1 bg-black/70 text-[8px] font-black text-white text-center py-1 rounded-md opacity-0 group-hover:opacity-100 transition uppercase">{{ labels[loop.index0] }}</span>
                                </div>
                                {% endfor %}
                            </div>
                        </div>
                        {% endfor %}

                        {% for ad in address_items %}
                        <div class="bg-[#1c2537] rounded-3xl p-8 border border-white/10 shadow-2xl">
                            <div class="flex items-center gap-3 mb-5">
                                <div class="w-8 h-8 bg-blue-500/10 rounded-lg flex items-center justify-center">
                                    <svg class="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17.657 16.657L13.414 20.9a1.998 1.998 0 01-2.827 0l-4.244-4.243a8 8 0 1111.314 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 11a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                                </div>
                                <div>
                                    <p class="label-caps !mb-0 text-blue-400">Assigned Address</p>
                                    <p class="text-[10px] text-gray-500 font-mono">{{ ad.timestamp }}</p>
                                </div>
                            </div>
                            <div class="bg-black/40 p-5 rounded-2xl border border-white/5">
                                <p class="text-white font-bold leading-relaxed">{{ ad.address_text }}</p>
                            </div>
                        </div>
                        {% endfor %}

                        {% for et in eta_items %}
                        {% set et_ts = et.timestamp.replace(' ', 'T')[:16] %}
                        <div class="bg-[#1c2537] rounded-3xl p-8 border border-white/10 shadow-2xl transmission-card">
                            <div class="card-actions">
                                <button type="button" class="act-btn act-btn-edit"
                                    onclick="openEditEta({{ et.id }}, '{{ et_ts }}', '{{ et.lat or "" }}', '{{ et.lng or "" }}', '{{ resolve_image_url(et.photo) }}')">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>
                                    Edit
                                </button>
                                <form action="/delete_eta/{{ et.id }}" method="POST" onsubmit="return confirm('Delete this ETA record?');">
                                    <button type="submit" class="act-btn act-btn-delete">
                                        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                                        Delete
                                    </button>
                                </form>
                            </div>
                            <div class="flex items-center gap-3 mb-5">
                                <div class="w-8 h-8 bg-green-500/10 rounded-lg flex items-center justify-center">
                                    <svg class="w-4 h-4 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
                                </div>
                                <div>
                                    <p class="label-caps !mb-0 text-green-400">ETA Confirmation</p>
                                    <p class="text-[10px] text-gray-500 font-mono">{{ et.timestamp }}</p>
                                </div>
                                {% if et.lat %}
                                <p class="ml-auto text-[9px] text-green-400/40 font-mono">{{ et.lat }}, {{ et.lng }}</p>
                                {% endif %}
                            </div>
                            <div class="flex gap-3">
                                <div class="relative group flex-shrink-0">
                                    <img src="{{ resolve_image_url(et.photo) }}" class="h-36 w-36 object-cover rounded-2xl border-2 border-white/5 group-hover:border-green-400 transition cursor-pointer shadow-lg" onclick="window.open(this.src)">
                                    <span class="absolute bottom-1 left-1 right-1 bg-black/70 text-[8px] font-black text-green-300 text-center py-1 rounded-md opacity-0 group-hover:opacity-100 transition uppercase">ETA Proof</span>
                                </div>
                                {% if et.lat %}
                                <div class="flex flex-col justify-center">
                                    <div class="bg-black/40 px-4 py-3 rounded-xl border border-white/5">
                                        <p class="label-caps !mb-0">Location</p>
                                        <p class="text-xs font-mono text-gray-300">{{ et.lat }}, {{ et.lng }}</p>
                                    </div>
                                </div>
                                {% endif %}
                            </div>
                        </div>
                        {% endfor %}

                        {% set break_starts = [] %}
                        {% set break_ends = [] %}
                        {% for br in break_items %}
                            {% if br.action == 'Start' %}{% set _ = break_starts.append(br) %}{% endif %}
                            {% if br.action == 'End' %}{% set _ = break_ends.append(br) %}{% endif %}
                        {% endfor %}

                        {% if break_items|length %}
                        <div class="bg-[#1c2537] rounded-3xl p-8 border border-white/10 shadow-2xl">
                            <div class="flex items-center gap-3 mb-6">
                                <div class="w-8 h-8 bg-orange-500/10 rounded-lg flex items-center justify-center">
                                    <svg class="w-4 h-4 text-orange-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                                </div>
                                <p class="label-caps !mb-0 text-orange-400">Break Record</p>
                            </div>
                            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                                {% if break_starts %}
                                {% set bs = break_starts[0] %}
                                {% set bs_ts = bs.timestamp.replace(' ', 'T')[:16] %}
                                <div class="space-y-3 transmission-card">
                                    <div class="card-actions">
                                        <button type="button" class="act-btn act-btn-edit"
                                            onclick="openEditBreak({{ bs.id }}, '{{ bs.action }}', '{{ bs_ts }}', '{{ bs.lat or "" }}', '{{ bs.lng or "" }}', '{{ resolve_image_url(bs.photo) }}')">
                                            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>
                                            Edit
                                        </button>
                                        <form action="/delete_break/{{ bs.id }}" method="POST" onsubmit="return confirm('Delete this break start record?');">
                                            <button type="submit" class="act-btn act-btn-delete">
                                                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                                                Delete
                                            </button>
                                        </form>
                                    </div>
                                    <p class="label-caps text-orange-400">Break Start</p>
                                    <p class="text-[10px] text-gray-500 font-mono">{{ bs.timestamp }}</p>
                                    {% if bs.lat %}<p class="text-[10px] text-orange-400/50 font-mono">{{ bs.lat }}, {{ bs.lng }}</p>{% endif %}
                                    <div class="relative group inline-block">
                                        <img src="{{ resolve_image_url(bs.photo) }}" class="h-36 w-36 object-cover rounded-2xl border-2 border-orange-400/20 group-hover:border-orange-400 transition cursor-pointer shadow-lg" onclick="window.open(this.src)">
                                        <span class="absolute bottom-1 left-1 right-1 bg-black/70 text-[8px] font-black text-orange-300 text-center py-1 rounded-md opacity-0 group-hover:opacity-100 transition uppercase">Break Start</span>
                                    </div>
                                </div>
                                {% endif %}

                                {% if break_ends %}
                                {% set be = break_ends[0] %}
                                {% set be_ts = be.timestamp.replace(' ', 'T')[:16] %}
                                <div class="space-y-3 transmission-card">
                                    <div class="card-actions">
                                        <button type="button" class="act-btn act-btn-edit"
                                            onclick="openEditBreak({{ be.id }}, '{{ be.action }}', '{{ be_ts }}', '{{ be.lat or "" }}', '{{ be.lng or "" }}', '{{ resolve_image_url(be.photo) }}')">
                                            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>
                                            Edit
                                        </button>
                                        <form action="/delete_break/{{ be.id }}" method="POST" onsubmit="return confirm('Delete this break end record?');">
                                            <button type="submit" class="act-btn act-btn-delete">
                                                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                                                Delete
                                            </button>
                                        </form>
                                    </div>
                                    <p class="label-caps text-gray-400">Break End</p>
                                    <p class="text-[10px] text-gray-500 font-mono">{{ be.timestamp }}</p>
                                    {% if be.lat %}<p class="text-[10px] text-gray-400/50 font-mono">{{ be.lat }}, {{ be.lng }}</p>{% endif %}
                                    <div class="relative group inline-block">
                                        <img src="{{ resolve_image_url(be.photo) }}" class="h-36 w-36 object-cover rounded-2xl border-2 border-white/5 group-hover:border-amber-400 transition cursor-pointer shadow-lg" onclick="window.open(this.src)">
                                        <span class="absolute bottom-1 left-1 right-1 bg-black/70 text-[8px] font-black text-white text-center py-1 rounded-md opacity-0 group-hover:opacity-100 transition uppercase">Break End</span>
                                    </div>
                                </div>
                                {% else %}
                                <div class="flex items-center justify-center h-36 rounded-2xl border-2 border-dashed border-white/10">
                                    <p class="text-xs text-gray-600 font-bold uppercase">Break still active</p>
                                </div>
                                {% endif %}
                            </div>
                        </div>
                        {% endif %}

                    </div>
                </div>
                {% endfor %}
            </div>
            {% endfor %}
            <div id="empty-state" class="glass-panel p-32 rounded-[4rem] text-center border-dashed border-4 border-white/5">
                <div class="w-24 h-24 bg-white/5 rounded-full flex items-center justify-center mx-auto mb-6">
                    <svg class="w-12 h-12 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" stroke-width="2"></path></svg>
                </div>
                <h2 class="text-2xl font-black text-gray-600 uppercase italic tracking-widest">Awaiting Identity Selection</h2>
                <p class="text-gray-500 mt-2 font-bold uppercase text-[10px] tracking-[3px]">Select personnel from sidebar to decrypt logs</p>
            </div>
        </div>
    </div>
</main>
<script>
function showUserLogs(btn, username) {
    document.querySelectorAll('.user-panel').forEach(p => p.classList.add('hidden'));
    document.getElementById('empty-state').classList.add('hidden');
    const panel = document.getElementById('panel-' + username);
    if(panel) panel.classList.remove('hidden');
    document.querySelectorAll('.sidebar-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
}
function filterSidebar() {
    let input = document.getElementById('userSearch').value.toLowerCase();
    document.querySelectorAll('.sidebar-btn').forEach(btn => {
        btn.style.display = btn.innerText.toLowerCase().includes(input) ? "flex" : "none";
    });
}
</script>
""" + EDIT_MODALS_HTML + CHAT_HTML + """
</body></html>"""

OP_HTML = f"<html>{COMMON_HEAD}<body class='pb-12'>" + NAV_BAR + DRAWER_HTML + """
<main class="px-6 max-w-4xl mx-auto">
    {% if assigned_address %}
    <div class="glass-panel p-5 rounded-2xl mb-6 border border-white/10">
        <div class="flex justify-between items-center">
            <p class="text-xs font-black uppercase tracking-widest">1st Address</p>
        </div>
        <div class="mt-3 text-sm text-gray-300">{{ assigned_address.address_text }}</div>
        <div class="mt-4">
            <button type="button" onclick="triggerETA()" class="btn-main px-5 py-3 text-xs tracking-widest">ETA</button>
        </div>
    </div>
    {% endif %}
    <div class="flex justify-end items-center gap-3 mb-6">
        {% if in_break %}
        <button type="button" onclick="triggerBreak('End')" class="btn-main px-5 py-3 text-xs tracking-widest bg-red-500 text-white hover:bg-red-400">End Break</button>
        {% else %}
        <button type="button" onclick="triggerBreak('Start')" class="btn-main px-5 py-3 text-xs tracking-widest">Start Break</button>
        {% endif %}
    </div>
    <div class="glass-panel p-5 rounded-2xl mb-8 border border-white/10">
        <div class="flex justify-between items-center">
            <p class="text-xs font-black uppercase tracking-widest">Break Status</p>
            <span class="text-xs font-bold text-amber-300">{{ 'ON BREAK' if in_break else 'OFF BREAK' }}</span>
        </div>
        <div class="mt-3 text-xs text-gray-300">Latest break action: {{ break_records[0].action if break_records else 'None yet' }} at {{ break_records[0].timestamp if break_records else 'N/A' }}</div>
        <div class="mt-4 max-h-40 overflow-y-auto">
            {% if break_records %}
            <table class="w-full text-xs border border-white/10 rounded-xl">
                <thead class="bg-white/5">
                    <tr><th class="px-2 py-1 text-left">Time</th><th class="px-2 py-1 text-left">Action</th><th class="px-2 py-1 text-left">Geo</th></tr>
                </thead>
                <tbody>
                    {% for br in break_records[:8] %}
                    <tr class="border-t border-white/10">
                        <td class="px-2 py-1">{{ br.timestamp }}</td>
                        <td class="px-2 py-1">{{ br.action }}</td>
                        <td class="px-2 py-1">{{ br.lat or 'N/A' }}, {{ br.lng or 'N/A' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <p class="text-[10px] text-gray-400 mt-2">No break records yet.</p>
            {% endif %}
        </div>
    </div>
    <form id="breakForm" action="/break_action" method="POST" enctype="multipart/form-data" class="hidden">
        <input type="hidden" id="breakAction" name="action" value="">
        <input type="hidden" id="breakLat" name="lat" value="">
        <input type="hidden" id="breakLng" name="lng" value="">
        <input type="file" id="breakPhotoInput" name="photo" accept="image/*" capture="user" class="hidden" onchange="submitBreakForm()" required>
    </form>
    <form id="etaForm" action="/submit_eta" method="POST" enctype="multipart/form-data" class="hidden">
        <input type="hidden" id="etaLat" name="lat" value="">
        <input type="hidden" id="etaLng" name="lng" value="">
        <input type="file" id="etaPhotoInput" name="photo" accept="image/*" capture="user" class="hidden" onchange="submitETAForm()" required>
    </form>
    <div id="breakStatus" class="text-right text-xs font-bold text-amber-400 mb-4"></div>

    <div class="mb-10">
        <h2 class="text-4xl font-black text-white italic uppercase tracking-tighter">Daily <span class="text-amber-400">Telemetry</span></h2>
        <p class="text-gray-500 font-bold uppercase text-[10px] tracking-[2px] mt-2">Field Report Submission Module</p>
    </div>
    <form method="POST" action="/submit" enctype="multipart/form-data" class="glass-panel p-10 rounded-[3rem] space-y-10 shadow-2xl">
        <input type="hidden" name="lat" id="lat">
        <input type="hidden" name="lng" id="lng">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-12">
            <div class="space-y-6">
                <div class="flex items-center gap-3 mb-2">
                    <div class="w-8 h-8 bg-amber-400/10 rounded-lg flex items-center justify-center text-amber-400 font-black italic">M</div>
                    <p class="label-caps !mb-0">Odometer Data</p>
                </div>
                <div class="space-y-4">
                    <div>
                        <label class="label-caps !text-[9px] opacity-50">Start Value</label>
                        <input type="number" name="start_mileage" class="input-field" placeholder="000000" required>
                        <input type="file" name="start_photo" accept="image/*" class="text-[10px] mt-3 block text-gray-500 font-bold" required>
                    </div>
                    <div class="pt-4">
                        <label class="label-caps !text-[9px] opacity-50">End Value</label>
                        <input type="number" name="end_mileage" class="input-field" placeholder="000000" required>
                        <input type="file" name="end_mileage_photo" accept="image/*" class="text-[10px] mt-3 block text-gray-500 font-bold" required>
                    </div>
                </div>
            </div>
            <div class="space-y-6">
                <div class="flex items-center gap-3 mb-2">
                    <div class="w-8 h-8 bg-amber-400/10 rounded-lg flex items-center justify-center text-amber-400 font-black italic">T</div>
                    <p class="label-caps !mb-0">Timekeeping</p>
                </div>
                <div class="space-y-4">
                    <div>
                        <label class="label-caps !text-[9px] opacity-50">Shift Initiation</label>
                        <input type="time" name="start_shift_time" class="input-field" required>
                        <input type="file" name="start_shift_photo" accept="image/*" class="text-[10px] mt-3 block text-gray-500 font-bold" required>
                    </div>
                    <div class="pt-4">
                        <label class="label-caps !text-[9px] opacity-50">Shift Termination</label>
                        <input type="time" name="end_shift_time" class="input-field" required>
                        <input type="file" name="end_shift_photo" accept="image/*" class="text-[10px] mt-3 block text-gray-500 font-bold" required>
                    </div>
                </div>
            </div>
        </div>
        <div class="flex items-center gap-4 bg-black/30 p-4 rounded-2xl border border-white/5">
            <p class="label-caps !mb-0 flex-grow">Visual Confirmation (ETA/Proof)</p>
            <input type="file" name="eta_img" accept="image/*" class="text-[10px] text-gray-500 font-bold" required>
        </div>
        <button type="submit" class="btn-main w-full py-6 text-2xl shadow-2xl shadow-amber-400/20">Finalize & Transmit Data</button>
    </form>
</main>
<script>
    navigator.geolocation.getCurrentPosition(
        p => { document.getElementById('lat').value = p.coords.latitude; document.getElementById('lng').value = p.coords.longitude; },
        e => console.log("GPS unavailable")
    );

    function triggerBreak(action) {
        const status = document.getElementById('breakStatus');
        status.textContent = `Preparing ${action.toLowerCase()} break image...`;
        document.getElementById('breakAction').value = action;
        if (navigator.geolocation) {
            navigator.geolocation.getCurrentPosition(pos => {
                document.getElementById('breakLat').value = pos.coords.latitude;
                document.getElementById('breakLng').value = pos.coords.longitude;
                status.textContent = `${action} break location captured.`;
                document.getElementById('breakPhotoInput').click();
            }, err => {
                document.getElementById('breakLat').value = 'N/A';
                document.getElementById('breakLng').value = 'N/A';
                status.textContent = 'Location unavailable; using N/A. Please take photo.';
                document.getElementById('breakPhotoInput').click();
            }, {enableHighAccuracy: true, timeout: 10000});
        } else {
            document.getElementById('breakLat').value = 'N/A';
            document.getElementById('breakLng').value = 'N/A';
            document.getElementById('breakPhotoInput').click();
        }
    }

    function submitBreakForm() {
        if (!document.getElementById('breakPhotoInput').files.length) {
            document.getElementById('breakStatus').textContent = 'No photo taken. Break aborted.';
            return;
        }
        document.getElementById('breakStatus').textContent = 'Uploading break evidence...';
        document.getElementById('breakForm').submit();
    }

    function triggerETA() {
        const status = document.getElementById('breakStatus');
        status.textContent = 'Preparing ETA photo...';
        if (navigator.geolocation) {
            navigator.geolocation.getCurrentPosition(pos => {
                document.getElementById('etaLat').value = pos.coords.latitude;
                document.getElementById('etaLng').value = pos.coords.longitude;
                status.textContent = 'ETA location captured.';
                document.getElementById('etaPhotoInput').click();
            }, err => {
                document.getElementById('etaLat').value = 'N/A';
                document.getElementById('etaLng').value = 'N/A';
                document.getElementById('etaPhotoInput').click();
            }, {enableHighAccuracy: true, timeout: 10000});
        } else {
            document.getElementById('etaLat').value = 'N/A';
            document.getElementById('etaLng').value = 'N/A';
            document.getElementById('etaPhotoInput').click();
        }
    }

    function submitETAForm() {
        if (!document.getElementById('etaPhotoInput').files.length) {
            document.getElementById('breakStatus').textContent = 'No photo taken. ETA aborted.';
            return;
        }
        document.getElementById('breakStatus').textContent = 'Uploading ETA photo...';
        document.getElementById('etaForm').submit();
    }
</script>
""" + CHAT_HTML + """
</body></html>"""

SUCCESS_HTML = f"<html>{COMMON_HEAD}<body class='flex items-center justify-center min-h-screen p-6'><div class='glass-panel p-16 rounded-[3rem] text-center max-w-md w-full border-b-8 border-amber-400'><div class='w-24 h-24 bg-amber-400 rounded-full flex items-center justify-center mx-auto mb-8 shadow-2xl shadow-amber-400/40'><svg class='w-12 h-12 text-black' fill='none' stroke='currentColor' viewBox='0 0 24 24'><path stroke-linecap='round' stroke-linejoin='round' stroke-width='4' d='M5 13l4 4L19 7'></path></svg></div><h1 class='text-4xl font-black text-white mb-4 uppercase italic tracking-tighter'>Data Synced</h1><p class='text-gray-500 font-bold uppercase text-xs tracking-[3px] mb-10'>Transmission Securely Filed</p><a href='/op' class='btn-main px-12 py-5 inline-block text-sm shadow-xl'>Return to Interface</a></div></body></html>"

@app.route("/test_slack", methods=["GET"])
@admin_required
def test_slack():
    """Test Slack DM with photo."""
    import glob
    # Find a real uploaded file
    files = glob.glob(os.path.join(UPLOAD_FOLDER, "*.jpg")) + glob.glob(os.path.join(UPLOAD_FOLDER, "*.png"))
    if files:
        filename = os.path.basename(files[0])
        notify_slack(f"🧪 TEST: Photo upload test from opscenter", filename)
        return f"Sent test photo: {filename}. Check Slack DMs!", 200
    else:
        notify_slack("🧪 TEST: Text-only test (no photos found)")
        return "Sent text-only test. Upload a photo first for full test.", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
