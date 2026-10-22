"""Database backups: serialising the SQLite database for download, and
uploading it to (and listing) an S3-compatible cloud backup bucket.

These back up the whole database, so they're shared by every module. The
admin views that use them are on ``CloudBackupStatusAdmin`` in
``core/admin.py``, and the ``cloud_backup`` management command also runs a cloud
backup; callers must enforce authorization themselves.
"""

import os
import sqlite3
import tempfile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import connections
from django.utils import timezone

from lifetracker.core.models import CLOUD_BACKUP_STATUS_PK, CloudBackupStatus

# The upload runs inside the request, so bound how long a hung endpoint can stall
# it: botocore's defaults (60s timeouts, several retries) can outlast gunicorn's
# 120s worker timeout. These are per socket operation, not for the whole upload.
CLOUD_BACKUP_S3_CONFIG = Config(
    connect_timeout=10,
    read_timeout=30,
    retries={"mode": "standard", "max_attempts": 2},
)


# What a cloud backup or listing raises for an expected failure (bad settings, S3
# errors), as opposed to a bug. Callers report these and carry on.
CLOUD_BACKUP_ERRORS = (BotoCoreError, ClientError, RuntimeError)


class CloudBackupStatusUpdateError(RuntimeError):
    """Raised when upload succeeds but persisting backup status fails."""

    def __init__(self, object_key):
        super().__init__("Cloud backup uploaded, but failed to record backup status.")
        self.object_key = object_key


def check_sqlite(feature):
    """Raise ``RuntimeError`` naming *feature* unless the database is SQLite."""
    engine = settings.DATABASES["default"]["ENGINE"]
    if engine != "django.db.backends.sqlite3":
        raise RuntimeError(f"{feature} is only available for SQLite databases.")


def backup_database():
    """Copy the whole default (SQLite) database into a temporary file.

    Returns the copy as a binary file open for reading from the start; the
    caller must close it. The file has no name left on disk, so it goes away
    once closed. Unlike ``Connection.serialize()``, this never holds the whole
    database in memory, as the database (which stores every attachment) can be
    bigger than the Machine's RAM.

    The online backup API reads pages through SQLite, so it includes
    transactions still in the write-ahead log that haven't been checkpointed
    into the main file yet. The copy's header keeps WAL mode, which SQLite
    resumes when it opens the file.

    Raises ``RuntimeError`` inside a transaction: the backup has to read through
    this connection to see its uncommitted writes, and SQLite refuses to back up
    from a connection that's writing, which ``backup()`` retries forever.
    """
    connection = connections["default"]
    if connection.in_atomic_block:
        raise RuntimeError("Can't back up the database inside a transaction.")
    with tempfile.TemporaryDirectory(prefix="lifetracker-backup-") as tmp_dir:
        path = os.path.join(tmp_dir, "backup.sqlite3")
        dest = sqlite3.connect(path)
        try:
            with connection.cursor() as cursor:
                cursor.connection.backup(dest)
        finally:
            dest.close()
        # Removing the directory unlinks the file, but this handle keeps its
        # content readable until it's closed.
        return open(path, "rb")


def backup_timestamp(when):
    """Format *when* as used in backup filenames: ``YYYY-MM-DD.HH.MM.SS``."""
    return when.strftime("%Y-%m-%d.%H.%M.%S")


def missing_cloud_backup_settings():
    """Return the names of the required cloud backup settings that are unset."""
    return [
        key
        for key, value in {
            "LIFETRACKER_BACKUP_S3_BUCKET_NAME": (
                settings.LIFETRACKER_BACKUP_S3_BUCKET_NAME
            ),
            "LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID": (
                settings.LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID
            ),
            "LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY": (
                settings.LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY
            ),
        }.items()
        if not value
    ]


def cloud_backup_s3_client():
    """Return a boto3 S3 client for the configured backup bucket."""
    endpoint_url = settings.LIFETRACKER_BACKUP_S3_ENDPOINT_URL or None
    region_name = settings.LIFETRACKER_BACKUP_S3_REGION_NAME or None
    try:
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            aws_access_key_id=settings.LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY,
            config=CLOUD_BACKUP_S3_CONFIG,
        )
    except ValueError as exc:
        # e.g. a malformed endpoint URL ("Invalid endpoint: ...").
        raise RuntimeError(f"Invalid S3 configuration: {exc}") from exc


def run_cloud_backup():
    """Upload the database to the cloud backup bucket; return its object key."""
    # Checked first: unlike missing settings, configuration can't fix this.
    check_sqlite("Cloud backup")

    missing_required = missing_cloud_backup_settings()
    if missing_required:
        raise RuntimeError(
            "Cloud backup settings are incomplete. Missing: "
            f"{', '.join(missing_required)}."
        )

    with backup_database() as backup_file:
        backup_size = os.fstat(backup_file.fileno()).st_size
        backup_time = timezone.now()
        object_key = (
            f"{settings.LIFETRACKER_BACKUP_HOSTNAME}/"
            f"db-backup.{backup_timestamp(backup_time)}.sqlite3"
        )
        s3_client = cloud_backup_s3_client()
        # Streams the file, in parts (a multipart upload) once it's big enough.
        s3_client.upload_fileobj(
            backup_file,
            settings.LIFETRACKER_BACKUP_S3_BUCKET_NAME,
            object_key,
            ExtraArgs={"ContentType": "application/x-sqlite3"},
        )
    try:
        CloudBackupStatus.objects.update_or_create(
            pk=CLOUD_BACKUP_STATUS_PK,
            defaults={
                "last_backup_at": backup_time,
                "last_backup_object_key": object_key,
                "last_backup_size": backup_size,
            },
        )
    except Exception as exc:
        raise CloudBackupStatusUpdateError(object_key) from exc
    return object_key


def list_cloud_backups(prefix):
    """List the bucket's top-level folders and the objects under *prefix*.

    Returns ``(folders, objects)``: the top-level folder prefixes (one per
    backup hostname), sorted, and the objects whose keys start with
    *prefix*, newest first.
    """
    s3_client = cloud_backup_s3_client()
    bucket = settings.LIFETRACKER_BACKUP_S3_BUCKET_NAME
    paginator = s3_client.get_paginator("list_objects_v2")
    folders = sorted(
        common_prefix["Prefix"]
        for page in paginator.paginate(Bucket=bucket, Delimiter="/")
        for common_prefix in page.get("CommonPrefixes", [])
    )
    objects = [
        {
            "key": obj["Key"],
            "size": obj["Size"],
            "last_modified": obj["LastModified"],
        }
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]
    objects.sort(key=lambda obj: (obj["last_modified"], obj["key"]), reverse=True)
    return folders, objects
