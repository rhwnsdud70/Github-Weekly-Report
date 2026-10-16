"""Opt-in R2 SQLite transfer. No pipeline, scheduler or API calls occur on import."""
from contextlib import closing
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from database.sqlite_backup import snapshot_database, validate_database, SQLiteBackupError


PRODUCTION_KEY = 'production/github_weekly_report.db'
BACKUP_PREFIX = 'backups/'
logger = logging.getLogger(__name__)


class R2PersistenceError(RuntimeError):
    def __init__(self, operation, category, http_status=None):
        self.operation, self.category, self.http_status = operation, category, http_status
        super().__init__(f'R2 {operation} failed: {category}; HTTP={http_status or "N/A"}')


def create_r2_client(settings):
    settings.validate_r2()
    # Lazy import keeps existing offline workflows independent of SDK availability.
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise R2PersistenceError('configure','SDK_NOT_INSTALLED') from None
    # SDK debug logs can include signed request headers. Never enable them here.
    for name in ('boto3','botocore','s3transfer'):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        return boto3.client('s3',endpoint_url=settings.r2_endpoint,region_name='auto',
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=Config(signature_version='s3v4',connect_timeout=10,read_timeout=60,
                          retries={'mode':'standard','total_max_attempts':3},
                          s3={'addressing_style':'path'}))
    except Exception:
        raise R2PersistenceError('configure','SDK_CONFIGURATION_ERROR') from None


def _safe_error(operation, error):
    """Allowlist categories only; arbitrary SDK messages/URLs/credentials are discarded."""
    response = getattr(error,'response',{})
    if not isinstance(response,dict):
        response = {}
    code = response.get('Error',{}).get('Code')
    status = response.get('ResponseMetadata',{}).get('HTTPStatusCode')
    status = status if isinstance(status,int) else None
    if code in {'AccessDenied','InvalidAccessKeyId','SignatureDoesNotMatch','ExpiredToken','InvalidToken'} or status in {401,403}:
        category = 'AUTHENTICATION_OR_PERMISSION'
    elif code == 'NoSuchBucket':
        category = 'BUCKET_NOT_FOUND'
    elif status == 412:
        category = 'REMOTE_OBJECT_CHANGED'
    elif status == 429 or code in {'SlowDown','Throttling'}:
        category = 'RATE_LIMIT'
    elif status is not None:
        category = 'HTTP_ERROR'
    elif type(error).__name__ in {'EndpointConnectionError','ConnectionClosedError','ConnectTimeoutError',
                                  'ReadTimeoutError','SSLError','ResponseStreamingError','IncompleteReadError'} or isinstance(error,(OSError,TimeoutError)):
        category = 'NETWORK_ERROR'
    else:
        category = 'SDK_ERROR'
    return R2PersistenceError(operation,category,status)


def _missing_key(error):
    response = getattr(error,'response',{})
    return (isinstance(response,dict) and response.get('Error',{}).get('Code') == 'NoSuchKey'
            and response.get('ResponseMetadata',{}).get('HTTPStatusCode') == 404)


class R2DatabaseStore:
    """Bind production object transfers to the configured production database path.

    Call restore before opening Database; call upload after closing ALL app handles.
    The surrounding runner must serialize jobs; R2 transfer is not a distributed lock.
    An injected client is owned by its caller; a lazily created client is closed here.
    """
    def __init__(self, settings, client=None):
        self.settings = settings
        self.database_path = Path(settings.database_path).resolve()
        name = self.database_path.name.lower()
        if any(marker in name for marker in ('integration','fixture','preview','dry-run','test')):
            raise SQLiteBackupError('Non-production database path; production transfer blocked')
        self._client = client
        self._owns_client = client is None

    @property
    def client(self):
        if self._client is None:
            self._client = create_r2_client(self.settings)
        return self._client

    def close(self):
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def restore(self):
        """Return False ONLY for NoSuchKey; never overwrite a local DB or its WAL.

        On first run no file is created: initialize through the existing Database class.
        Corrupt downloads are removed; all failures leave existing local state untouched.
        """
        path = self.database_path
        if path.exists() or any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
            raise SQLiteBackupError('Local SQLite state already exists; restore blocked')
        path.parent.mkdir(parents=True,exist_ok=True)
        client = self.client
        try:
            response = client.get_object(Bucket=self.settings.r2_bucket,Key=PRODUCTION_KEY)
        except Exception as error:
            if _missing_key(error):
                logger.info('R2 production object absent: first-run initialization required')
                return False
            raise _safe_error('download',error) from None
        with TemporaryDirectory(prefix='.r2-restore-',dir=path.parent) as directory:
            temporary = Path(directory)/'database.db'
            try:
                with closing(response['Body']) as body, temporary.open('wb') as output:
                    while chunk := body.read(1024*1024):
                        output.write(chunk)
            except Exception as error:
                raise _safe_error('download',error) from None
            validate_database(temporary)
            # Hard-link publishes the validated file atomically and refuses a racing
            # local creator, unlike os.replace which could overwrite its new claim.
            os.link(temporary,path)
        logger.info('R2 SQLite restore completed; integrity validated')
        return True

    def upload(self):
        """Validate/export locally, back up old remote object, then upload the new DB.

        Backup keys are UTC timestamp + UUID. Copy failure is fatal except NoSuchKey
        on the very first upload. No credentials or raw SDK errors are ever logged.
        Returns the backup key, or None if no prior production object existed.
        """
        path = self.database_path
        if not path.is_file():
            raise SQLiteBackupError('SQLite file missing or empty')
        with TemporaryDirectory(prefix='.r2-upload-',dir=path.parent) as directory:
            snapshot = Path(directory)/'database.db'
            snapshot_database(path,snapshot)
            client = self.client
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup_key = f'{BACKUP_PREFIX}{stamp}-{uuid4().hex}.db'
            try:
                client.copy_object(Bucket=self.settings.r2_bucket,Key=backup_key,
                    CopySource={'Bucket':self.settings.r2_bucket,'Key':PRODUCTION_KEY})
            except Exception as error:
                if not _missing_key(error):
                    raise _safe_error('backup',error) from None
                backup_key = None
            try:
                with snapshot.open('rb') as body:
                    client.put_object(Bucket=self.settings.r2_bucket,Key=PRODUCTION_KEY,
                                      Body=body,ContentType='application/vnd.sqlite3')
            except Exception as error:
                raise _safe_error('upload',error) from None
        logger.info('R2 SQLite upload completed; previous remote backup=%s',bool(backup_key))
        return backup_key
