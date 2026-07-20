"""
Shared enums used by both the SQLAlchemy models (as native PostgreSQL enum
types) and the Pydantic schemas (as request/response validation). Defining
these once and importing them in both places is what makes "enforced at the
Pydantic layer AND the PostgreSQL layer" a single source of truth rather than
two lists that can drift apart.
"""
import enum


class SourceType(str, enum.Enum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    REST_API = "rest_api"
    CSV_UPLOAD = "csv_upload"
    S3 = "s3"
    OTHER = "other"


class TaskType(str, enum.Enum):
    SYNC = "sync"
    TRANSFORM = "transform"
    EXPORT = "export"
    OTHER = "other"


class TaskRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
