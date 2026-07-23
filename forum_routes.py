"""
Standalone friend forum -- built at Eric's request as a small phpBB-style
message board "built into this [repo], but with no attachment to the rest of
everything." That constraint is taken literally throughout this file:

  - Own Flask Blueprint (forum_bp), mounted at /forum, registered in
    mainbot.py alongside (not merged into) web_bp.
  - Own MongoDB *database* (graveyardbot_forum, a different logical database
    than the "graveyardbot" one everything else in this repo uses, even
    though it's the same physical Mongo cluster/connection string) with its
    own collections (users/categories/threads/posts). Nothing here reads or
    writes any collection the clan-management side of the app uses.
  - Own auth: a plain username + password account system with its own
    session key (session["forum_user"]) and its own password hashing,
    completely separate from the Discord OAuth login used everywhere else in
    this repo. Logging into the forum does not log you into the roster/admin
    side, and vice versa.
  - Own CSRF token (session["forum_csrf"]), not the one web_routes.py mints.
  - Own templates (templates/forum/*.html) with self-contained inline CSS --
    no dependency on static/theme.css or any class/variable name defined by
    the rest of the site, so restyling one can never break the other.
  - Zero imports from web_routes.py or data_harvester.py, and neither of
    those files imports anything from here. The only thing shared with the
    rest of the app is the Mongo connection string and Redis are NOT even
    shared -- this file makes its own MongoClient.

Deliberately simple by design (this is a small forum for friends, not a
public message board): no email verification, no password reset flow, no
rate limiting on login attempts. The first account ever created on this
forum automatically becomes a forum admin (able to create categories and
moderate threads/posts) -- there's no separate invite system to configure.
"""
import os
import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, request, session, redirect, url_for, render_template, abort, flash
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
from bson import ObjectId
from bson.errors import InvalidId

forum_bp = Blueprint("forum_bp", __name__, url_prefix="/forum", template_folder="templates/forum")

# ---------------------------------------------------------------------------
# Its own Mongo connection, its own database. Same MONGO_URL env var (that's
# just infrastructure -- which cluster to talk to), but a distinct database
# name so there is no chance of a collection name colliding with anything
# the clan-management side of the app reads or writes.
# ---------------------------------------------------------------------------
_forum_mongo_client = MongoClient(os.getenv("MONGO_URL", "mongodb://localhost:27017"))
forum_db = _forum_mongo_client["graveyardbot_forum"]

MAX_USERNAME_LEN = 24
MIN_PASSWORD_LEN = 8
MAX_TITLE_LEN = 150
MAX_BODY_LEN = 10000


def _now():
    return datetime.now(timezone.utc)


def _oid(raw_id):
    """ObjectId(...) raises ValueError/InvalidId on a malformed id -- treat
    that the same as "not found" (404) rather than letting it bubble up as a
    500, since these ids come straight from the URL."""
    try:
        return ObjectId(raw_id)
    except (InvalidId, TypeError):
        return None


def current_forum_user():
    return session.get("forum_user")


def forum_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_forum_user():
            flash("Log in to do that.", "error")
            return redirect(url_for("forum_bp.forum_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def forum_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_forum_user()
        if not user or not user.get("is_admin"):
            return "Forum admin only.", 403
        return view(*args, **kwargs)
    return wrapped


def _csrf_token():
    if "forum_csrf" not in session:
        session["forum_csrf"] = secrets.token_hex(16)
    return session["forum_csrf"]


def _csrf_valid():
    return request.form.get("csrf_token") and request.form.get("csrf_token") == session.get("forum_csrf")


@forum_bp.app_template_global()
def forum_csrf_token():
    """Available as {{ forum_csrf_token() }} in templates/forum/*.html."""
    return _csrf_token()


# ---------------------------------------------------------------------------
# Categories / index
# ---------------------------------------------------------------------------
@forum_bp.route("/")
def forum_index():
    categories = list(forum_db["categories"].find().sort("order", 1))
    for cat in categories:
        cat["thread_count"] = forum_db["threads"].count_documents({"category_id": cat["_id"]})
        last_thread = forum_db["threads"].find_one({"category_id": cat["_id"]}, sort=[("last_post_at", -1)])
        cat["last_activity_at"] = last_thread.get("last_post_at") if last_thread else None
    return render_template("index.html", categories=categories, user=current_forum_user())


@forum_bp.route("/category/new", methods=["POST"])
@forum_admin_required
def forum_new_category():
    if not _csrf_valid():
        abort(403)
    name = request.form.get("name", "").strip()[:60]
    description = request.form.get("description", "").strip()[:200]
    if not name:
        flash("A category needs a name.", "error")
        return redirect(url_for("forum_bp.forum_index"))
    forum_db["categories"].insert_one({
        "name": name,
        "description": description,
        "order": forum_db["categories"].count_documents({}),
        "created_at": _now(),
    })
    flash(f'Category "{name}" created.', "ok")
    return redirect(url_for("forum_bp.forum_index"))


@forum_bp.route("/category/<category_id>/delete", methods=["POST"])
@forum_admin_required
def forum_delete_category(category_id):
    if not _csrf_valid():
        abort(403)
    cat_oid = _oid(category_id)
    if not cat_oid:
        abort(404)
    thread_ids = [t["_id"] for t in forum_db["threads"].find({"category_id": cat_oid}, {"_id": 1})]
    forum_db["posts"].delete_many({"thread_id": {"$in": thread_ids}})
    forum_db["threads"].delete_many({"category_id": cat_oid})
    forum_db["categories"].delete_one({"_id": cat_oid})
    flash("Category and everything in it deleted.", "ok")
    return redirect(url_for("forum_bp.forum_index"))


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------
@forum_bp.route("/category/<category_id>")
def forum_category(category_id):
    cat_oid = _oid(category_id)
    if not cat_oid:
        abort(404)
    category = forum_db["categories"].find_one({"_id": cat_oid})
    if not category:
        abort(404)
    threads = list(forum_db["threads"].find({"category_id": cat_oid}).sort([("pinned", -1), ("last_post_at", -1)]))
    return render_template("category.html", category=category, threads=threads, user=current_forum_user())


@forum_bp.route("/category/<category_id>/new-thread", methods=["GET", "POST"])
@forum_login_required
def forum_new_thread(category_id):
    cat_oid = _oid(category_id)
    if not cat_oid:
        abort(404)
    category = forum_db["categories"].find_one({"_id": cat_oid})
    if not category:
        abort(404)

    if request.method == "POST":
        if not _csrf_valid():
            abort(403)
        title = request.form.get("title", "").strip()[:MAX_TITLE_LEN]
        body = request.form.get("body", "").strip()[:MAX_BODY_LEN]
        if not title or not body:
            flash("A thread needs both a title and a first post.", "error")
            return render_template("new_thread.html", category=category, user=current_forum_user(),
                                    prefill_title=title, prefill_body=body)
        user = current_forum_user()
        now = _now()
        thread_id = forum_db["threads"].insert_one({
            "category_id": cat_oid,
            "title": title,
            "author_username": user["username"],
            "created_at": now,
            "last_post_at": now,
            "post_count": 1,
            "pinned": False,
            "locked": False,
        }).inserted_id
        forum_db["posts"].insert_one({
            "thread_id": thread_id,
            "author_username": user["username"],
            "body": body,
            "created_at": now,
        })
        return redirect(url_for("forum_bp.forum_thread", thread_id=str(thread_id)))

    return render_template("new_thread.html", category=category, user=current_forum_user(),
                            prefill_title="", prefill_body="")


@forum_bp.route("/thread/<thread_id>")
def forum_thread(thread_id):
    thread_oid = _oid(thread_id)
    if not thread_oid:
        abort(404)
    thread = forum_db["threads"].find_one({"_id": thread_oid})
    if not thread:
        abort(404)
    category = forum_db["categories"].find_one({"_id": thread["category_id"]})
    posts = list(forum_db["posts"].find({"thread_id": thread_oid}).sort("created_at", 1))
    return render_template("thread.html", thread=thread, category=category, posts=posts,
                            user=current_forum_user())


@forum_bp.route("/thread/<thread_id>/reply", methods=["POST"])
@forum_login_required
def forum_reply(thread_id):
    if not _csrf_valid():
        abort(403)
    thread_oid = _oid(thread_id)
    if not thread_oid:
        abort(404)
    thread = forum_db["threads"].find_one({"_id": thread_oid})
    if not thread:
        abort(404)
    user = current_forum_user()
    if thread.get("locked") and not user.get("is_admin"):
        flash("This thread is locked.", "error")
        return redirect(url_for("forum_bp.forum_thread", thread_id=thread_id))
    body = request.form.get("body", "").strip()[:MAX_BODY_LEN]
    if not body:
        flash("Can't post an empty reply.", "error")
        return redirect(url_for("forum_bp.forum_thread", thread_id=thread_id))
    now = _now()
    forum_db["posts"].insert_one({
        "thread_id": thread_oid,
        "author_username": user["username"],
        "body": body,
        "created_at": now,
    })
    forum_db["threads"].update_one({"_id": thread_oid}, {"$set": {"last_post_at": now}, "$inc": {"post_count": 1}})
    return redirect(url_for("forum_bp.forum_thread", thread_id=thread_id))


@forum_bp.route("/thread/<thread_id>/pin", methods=["POST"])
@forum_admin_required
def forum_pin_thread(thread_id):
    if not _csrf_valid():
        abort(403)
    thread_oid = _oid(thread_id)
    if not thread_oid:
        abort(404)
    thread = forum_db["threads"].find_one({"_id": thread_oid})
    if not thread:
        abort(404)
    forum_db["threads"].update_one({"_id": thread_oid}, {"$set": {"pinned": not thread.get("pinned", False)}})
    return redirect(url_for("forum_bp.forum_thread", thread_id=thread_id))


@forum_bp.route("/thread/<thread_id>/lock", methods=["POST"])
@forum_admin_required
def forum_lock_thread(thread_id):
    if not _csrf_valid():
        abort(403)
    thread_oid = _oid(thread_id)
    if not thread_oid:
        abort(404)
    thread = forum_db["threads"].find_one({"_id": thread_oid})
    if not thread:
        abort(404)
    forum_db["threads"].update_one({"_id": thread_oid}, {"$set": {"locked": not thread.get("locked", False)}})
    return redirect(url_for("forum_bp.forum_thread", thread_id=thread_id))


@forum_bp.route("/thread/<thread_id>/delete", methods=["POST"])
@forum_admin_required
def forum_delete_thread(thread_id):
    if not _csrf_valid():
        abort(403)
    thread_oid = _oid(thread_id)
    if not thread_oid:
        abort(404)
    thread = forum_db["threads"].find_one({"_id": thread_oid})
    if not thread:
        abort(404)
    category_id = str(thread["category_id"])
    forum_db["posts"].delete_many({"thread_id": thread_oid})
    forum_db["threads"].delete_one({"_id": thread_oid})
    flash("Thread deleted.", "ok")
    return redirect(url_for("forum_bp.forum_category", category_id=category_id))


@forum_bp.route("/post/<post_id>/delete", methods=["POST"])
@forum_admin_required
def forum_delete_post(post_id):
    if not _csrf_valid():
        abort(403)
    post_oid = _oid(post_id)
    if not post_oid:
        abort(404)
    post = forum_db["posts"].find_one({"_id": post_oid})
    if not post:
        abort(404)
    thread = forum_db["threads"].find_one({"_id": post["thread_id"]})
    # Deleting a thread's very first post would leave an orphaned thread with
    # no opening post -- point admins at "delete thread" instead for that case.
    if thread:
        first_post = forum_db["posts"].find_one({"thread_id": thread["_id"]}, sort=[("created_at", 1)])
        if first_post and first_post["_id"] == post_oid:
            flash("Can't delete a thread's first post on its own — delete the whole thread instead.", "error")
            return redirect(url_for("forum_bp.forum_thread", thread_id=str(thread["_id"])))
    forum_db["posts"].delete_one({"_id": post_oid})
    if thread:
        forum_db["threads"].update_one({"_id": thread["_id"]}, {"$inc": {"post_count": -1}})
        return redirect(url_for("forum_bp.forum_thread", thread_id=str(thread["_id"])))
    return redirect(url_for("forum_bp.forum_index"))


# ---------------------------------------------------------------------------
# Auth (own account system, own session key -- unrelated to Discord OAuth)
# ---------------------------------------------------------------------------
@forum_bp.route("/register", methods=["GET", "POST"])
def forum_register():
    if current_forum_user():
        return redirect(url_for("forum_bp.forum_index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        error = None
        if not (2 <= len(username) <= MAX_USERNAME_LEN) or not username.replace("_", "").replace("-", "").isalnum():
            error = f"Username must be 2-{MAX_USERNAME_LEN} characters (letters, numbers, - and _ only)."
        elif len(password) < MIN_PASSWORD_LEN:
            error = f"Password must be at least {MIN_PASSWORD_LEN} characters."
        elif password != confirm:
            error = "Passwords don't match."
        elif forum_db["users"].find_one({"username_lower": username.lower()}):
            error = "That username is already taken."
        if error:
            flash(error, "error")
            return render_template("register.html", user=None, prefill_username=username)

        is_first_ever = forum_db["users"].count_documents({}) == 0
        forum_db["users"].insert_one({
            "username": username,
            "username_lower": username.lower(),
            "password_hash": generate_password_hash(password),
            "is_admin": is_first_ever,
            "created_at": _now(),
        })
        session["forum_user"] = {"username": username, "is_admin": is_first_ever}
        if is_first_ever:
            flash(f"Welcome, {username} — as the first account here, you're a forum admin.", "ok")
        return redirect(url_for("forum_bp.forum_index"))
    return render_template("register.html", user=None, prefill_username="")


@forum_bp.route("/login", methods=["GET", "POST"])
def forum_login():
    if current_forum_user():
        return redirect(url_for("forum_bp.forum_index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = forum_db["users"].find_one({"username_lower": username.lower()})
        if user and check_password_hash(user["password_hash"], password):
            session["forum_user"] = {"username": user["username"], "is_admin": user.get("is_admin", False)}
            next_path = request.args.get("next", "")
            if next_path and next_path.startswith("/forum/"):
                return redirect(next_path)
            return redirect(url_for("forum_bp.forum_index"))
        flash("Wrong username or password.", "error")
        return render_template("login.html", user=None, prefill_username=username)
    return render_template("login.html", user=None, prefill_username="")


@forum_bp.route("/logout", methods=["POST"])
def forum_logout():
    session.pop("forum_user", None)
    return redirect(url_for("forum_bp.forum_index"))
