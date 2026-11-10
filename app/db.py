import datetime as dt

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, validates

from .dates import normalize as normalize_date

from .config import DATABASE_URL

_is_sqlite = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=True,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


STATUSES = ["saved", "referral", "applied", "interviewing", "offer", "closed"]


class User(Base):
    """A profile. No passwords per profile — the app login guards everything,
    profiles just keep two people's job lists apart."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    # Free text: target roles, must-haves, dealbreakers. Used by the fit score.
    preferences: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    jobs = relationship("Job", back_populates="user", cascade="all, delete-orphan")
    personas = relationship("Persona", back_populates="user", cascade="all, delete-orphan")


class Persona(Base):
    """A base resume. Most people need one; some keep a version per role type."""
    __tablename__ = "personas"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    resume_text: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    user = relationship("User", back_populates="personas")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("user_id", "url", name="uq_job_user_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(300), default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    salary: Mapped[str] = mapped_column(String(200), default="")
    employment_type: Mapped[str] = mapped_column(String(100), default="")
    posted_date: Mapped[str] = mapped_column(String(50), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(50), default="manual")  # how it got here
    status: Mapped[str] = mapped_column(String(20), default="saved", index=True)
    tags: Mapped[str] = mapped_column(String(300), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fit_json: Mapped[str] = mapped_column(Text, default="")
    queued: Mapped[bool] = mapped_column(Boolean, default=False)  # on the overnight list
    # Set when an import shows the posting went up again under the same link.
    reposted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    first_posted: Mapped[str] = mapped_column(String(50), default="", server_default="")  # posted date before the first repost
    # Archived jobs keep their stage but are hidden from the everyday lists and overnight runs.
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    applied_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user = relationship("User", back_populates="jobs")

    @validates("posted_date")
    def _clean_posted(self, _key, value):
        # Store one format (YYYY-MM-DD) whatever the source wrote; "3 days ago"
        # is counted back from when the job was saved.
        return normalize_date(value, self.created_at or now())

    documents = relationship(
        "Document", back_populates="job", cascade="all, delete-orphan",
        order_by="Document.created_at.desc()",
    )


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # "resume" | "cover_letter"
    content: Mapped[str] = mapped_column(Text, default="")
    persona_name: Mapped[str] = mapped_column(String(120), default="")
    engine: Mapped[str] = mapped_column(String(120), default="")  # e.g. "Local (qwen3:14b)"
    nightly: Mapped[bool] = mapped_column(Boolean, default=False)  # made by the overnight run
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)

    job = relationship("Job", back_populates="documents")


class NightlySetting(Base):
    """Per-profile overnight run: make resumes / fit checks for saved jobs."""
    __tablename__ = "nightly_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    run_time: Mapped[str] = mapped_column(String(5), default="01:00")  # HH:MM, local time
    engine: Mapped[str] = mapped_column(String(20), default="local")
    do_resume: Mapped[bool] = mapped_column(Boolean, default=True)
    do_fit: Mapped[bool] = mapped_column(Boolean, default=True)
    do_cover: Mapped[bool] = mapped_column(Boolean, default=False)
    max_jobs: Mapped[int] = mapped_column(Integer, default=40, server_default="40")  # jobs per run
    stop_at: Mapped[str] = mapped_column(String(5), default="", server_default="")  # HH:MM: finish by then, "" = no limit
    last_run_date: Mapped[str] = mapped_column(String(10), default="")  # YYYY-MM-DD, avoids double runs


class NightlyRun(Base):
    __tablename__ = "nightly_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")  # schedule | manual
    engine: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | done | failed | stopped
    made: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of {job_id, title, task, ok, note}
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


# Columns added after the first release. create_all() makes new tables but
# never alters existing ones, so add missing columns here (SQLite-safe).
_ADDED_COLUMNS = {
    "documents": {"engine": "VARCHAR(120) DEFAULT ''", "nightly": "BOOLEAN DEFAULT 0"},
    "nightly_settings": {"max_jobs": "INTEGER DEFAULT 40", "stop_at": "VARCHAR(5) DEFAULT ''"},
    "jobs": {"queued": "BOOLEAN DEFAULT 0", "reposted_at": "DATETIME", "first_posted": "VARCHAR(50) DEFAULT ''",
             "archived_at": "DATETIME"},
}


def _migrate():
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            if not insp.has_table(table):
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _fix_posted_dates():
    """Older versions stored posted dates as written ("2026-04-27T00:00:00.000Z",
    "3 days ago"). Rewrite them as YYYY-MM-DD with plain SQL, so the jobs'
    "updated" time isn't touched."""
    from sqlalchemy import text
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, posted_date, created_at FROM jobs WHERE posted_date != '' "
            "AND (length(posted_date) != 10 OR posted_date NOT LIKE '____-__-__')")).fetchall()
        for jid, raw, created in rows:
            ref = created if isinstance(created, dt.datetime) else (
                dt.datetime.fromisoformat(str(created)) if created else None)
            fixed = normalize_date(raw, ref)
            if fixed != raw:
                conn.execute(text("UPDATE jobs SET posted_date = :d WHERE id = :i"), {"d": fixed, "i": jid})


def init_db():
    Base.metadata.create_all(engine)
    _migrate()
    _fix_posted_dates()
    with SessionLocal() as s:
        if not s.query(User).first():
            s.add(User(name="Me"))
            s.commit()


def get_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
