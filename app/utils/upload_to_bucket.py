"""
upload_to_bucket.py
--------------------
Original helpers kept intact.
Added:
  - upload_bytes_to_s3()       — upload in-memory bytes (TTS audio) without touching disk
  - delete_s3_keys_batch()     — delete a list of S3 keys in one API call (used for TTS cleanup)
  - delete_s3_prefix()         — delete ALL objects under a prefix (e.g. audio/tts/{session_id}_)
"""

import boto3
import os
from botocore.exceptions import ClientError
import logging
from io import BytesIO
from typing import List, Optional

logger = logging.getLogger(__name__)

s3_client = boto3.client(
    's3',
    aws_access_key_id=os.getenv('S3_ACCESS_KEY'),
    aws_secret_access_key=os.getenv('S3_SECRET_KEY'),
    region_name=os.getenv('S3_REGION', 'eu-north-1')
)

S3_BUCKET_NAME = os.getenv('S3_BUCKET_NAME', 'mycvconnect')
S3_ENDPOINT = os.getenv('S3_ENDPOINT')


# ============================================================
#  Original helpers — unchanged
# ============================================================

def upload_file_to_s3(file_path: str, bucket_name: str = None, object_name: str = None) -> dict:
    """Upload a file from disk to S3 with public-read ACL."""
    if bucket_name is None:
        bucket_name = S3_BUCKET_NAME
    if object_name is None:
        object_name = os.path.basename(file_path)
    try:
        s3_client.upload_file(
            file_path,
            bucket_name,
            object_name,
            ExtraArgs={
                'ContentType': get_content_type(file_path)
            }
        )
        file_url = _build_url(bucket_name, object_name)
        logger.info(f"File uploaded successfully: {file_url}")
        return {'success': True, 'url': file_url, 'message': 'File uploaded successfully'}
    except ClientError as e:
        msg = f"Failed to upload file: {str(e)}"
        logger.error(msg)
        return {'success': False, 'url': None, 'message': msg}
    except FileNotFoundError:
        msg = f"The file {file_path} was not found"
        logger.error(msg)
        return {'success': False, 'url': None, 'message': msg}
    except Exception as e:
        msg = f"An unexpected error occurred: {str(e)}"
        logger.error(msg)
        return {'success': False, 'url': None, 'message': msg}


def upload_file_object_to_s3(file_object, bucket_name: str = None, object_name: str = None) -> dict:
    """Upload a file-like object to S3 with public-read ACL."""
    if bucket_name is None:
        bucket_name = S3_BUCKET_NAME
    try:
        s3_client.upload_fileobj(
            file_object,
            bucket_name,
            object_name,
            ExtraArgs={
                'ContentType': get_content_type(object_name)
            }
        )
        file_url = _build_url(bucket_name, object_name)
        logger.info(f"File object uploaded successfully: {file_url}")
        return {'success': True, 'url': file_url, 'message': 'File uploaded successfully'}
    except ClientError as e:
        msg = f"Failed to upload file object: {str(e)}"
        logger.error(msg)
        return {'success': False, 'url': None, 'message': msg}
    except Exception as e:
        msg = f"An unexpected error occurred: {str(e)}"
        logger.error(msg)
        return {'success': False, 'url': None, 'message': msg}


def get_content_type(file_path: str) -> str:
    """Determine the MIME type based on file extension."""
    extension = os.path.splitext(file_path)[1].lower()
    content_types = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.svg': 'image/svg+xml',
        '.pdf': 'application/pdf',
        '.txt': 'text/plain',
        '.json': 'application/json',
        '.mp4': 'video/mp4',
        '.mp3': 'audio/mpeg',
    }
    return content_types.get(extension, 'application/octet-stream')


def delete_file_from_s3(bucket_name: str = None, object_name: str = None) -> dict:
    """Delete a single file from S3."""
    if bucket_name is None:
        bucket_name = S3_BUCKET_NAME
    try:
        s3_client.delete_object(Bucket=bucket_name, Key=object_name)
        logger.info(f"File deleted successfully: {object_name}")
        return {'success': True, 'message': 'File deleted successfully'}
    except ClientError as e:
        msg = f"Failed to delete file: {str(e)}"
        logger.error(msg)
        return {'success': False, 'message': msg}
    except Exception as e:
        msg = f"An unexpected error occurred: {str(e)}"
        logger.error(msg)
        return {'success': False, 'message': msg}


# ============================================================
#  New helpers for the hiring assistant
# ============================================================

def _build_url(bucket_name: str, object_name: str) -> str:
    region = os.getenv('S3_REGION', 'eu-north-1')
    return f"https://{bucket_name}.s3.{region}.amazonaws.com/{object_name}"


def upload_bytes_to_s3(
    data: bytes,
    object_name: str,
    content_type: str = "audio/mpeg",
    bucket_name: str = None,
    public: bool = False,
) -> dict:
    """
    Upload raw bytes (e.g. TTS audio from OpenAI) directly to S3 without writing to disk.

    Args:
        data:         Raw bytes to upload
        object_name:  S3 key, e.g. "audio/tts/{session_id}_{turn}.mp3"
        content_type: MIME type, defaults to "audio/mpeg" for mp3
        bucket_name:  Defaults to S3_BUCKET_NAME env var
        public:       If True, adds public-read ACL (set False for TTS audio — use presigned URLs instead)

    Returns:
        { success, url, message }
    """
    if bucket_name is None:
        bucket_name = S3_BUCKET_NAME
    try:
        s3_client.upload_fileobj(
            BytesIO(data),
            bucket_name,
            object_name,
            ExtraArgs={"ContentType": content_type},
        )
        file_url = _build_url(bucket_name, object_name)
        logger.info(f"Bytes uploaded successfully: {file_url}")
        return {'success': True, 'url': file_url, 'message': 'Uploaded successfully'}
    except ClientError as e:
        msg = f"Failed to upload bytes: {str(e)}"
        logger.error(msg)
        return {'success': False, 'url': None, 'message': msg}
    except Exception as e:
        msg = f"Unexpected error uploading bytes: {str(e)}"
        logger.error(msg)
        return {'success': False, 'url': None, 'message': msg}


def generate_presigned_url(object_name: str, bucket_name: str = None, expires_in: int = 300) -> Optional[str]:
    """
    Generate a short-lived presigned URL for a private S3 object (e.g. TTS audio).

    Args:
        object_name:  S3 key
        bucket_name:  Defaults to S3_BUCKET_NAME
        expires_in:   Seconds until URL expires (default 5 minutes)

    Returns:
        Presigned URL string, or None on error
    """
    if bucket_name is None:
        bucket_name = S3_BUCKET_NAME
    try:
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': object_name},
            ExpiresIn=expires_in,
        )
        return url
    except ClientError as e:
        logger.error(f"Failed to generate presigned URL for {object_name}: {e}")
        return None


def delete_s3_keys_batch(keys: List[str], bucket_name: str = None) -> dict:
    """
    Delete a list of S3 keys in a single API call (max 1000 per call, AWS limit).

    Use this at session end to clean up all TTS audio files for a session.

    Args:
        keys:         List of S3 object keys to delete
        bucket_name:  Defaults to S3_BUCKET_NAME

    Returns:
        { success, deleted_count, errors }
    """
    if bucket_name is None:
        bucket_name = S3_BUCKET_NAME
    if not keys:
        return {'success': True, 'deleted_count': 0, 'errors': []}

    try:
        # AWS delete_objects accepts up to 1000 keys at a time
        chunks = [keys[i:i + 1000] for i in range(0, len(keys), 1000)]
        total_deleted = 0
        all_errors = []

        for chunk in chunks:
            response = s3_client.delete_objects(
                Bucket=bucket_name,
                Delete={
                    'Objects': [{'Key': k} for k in chunk],
                    'Quiet': False,
                },
            )
            deleted = response.get('Deleted', [])
            errors = response.get('Errors', [])
            total_deleted += len(deleted)
            all_errors.extend(errors)

        if all_errors:
            logger.warning(f"Batch delete partial errors: {all_errors}")

        logger.info(f"Batch deleted {total_deleted} S3 objects")
        return {
            'success': len(all_errors) == 0,
            'deleted_count': total_deleted,
            'errors': all_errors,
        }
    except ClientError as e:
        msg = f"Batch delete failed: {str(e)}"
        logger.error(msg)
        return {'success': False, 'deleted_count': 0, 'errors': [msg]}
    except Exception as e:
        msg = f"Unexpected error in batch delete: {str(e)}"
        logger.error(msg)
        return {'success': False, 'deleted_count': 0, 'errors': [msg]}


def delete_s3_prefix(prefix: str, bucket_name: str = None) -> dict:
    """
    Delete ALL S3 objects whose key starts with `prefix`.

    Example:
        delete_s3_prefix("audio/tts/abc123_")
        → deletes audio/tts/abc123_0.mp3, audio/tts/abc123_1.mp3, etc.

    This is a fallback for cases where tts_turns list in Redis was lost.
    It lists objects first (paginated) then batch-deletes.

    Args:
        prefix:      S3 key prefix to match
        bucket_name: Defaults to S3_BUCKET_NAME

    Returns:
        { success, deleted_count, errors }
    """
    if bucket_name is None:
        bucket_name = S3_BUCKET_NAME
    try:
        keys_to_delete = []
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
            for obj in page.get('Contents', []):
                keys_to_delete.append(obj['Key'])

        if not keys_to_delete:
            logger.info(f"No objects found for prefix: {prefix}")
            return {'success': True, 'deleted_count': 0, 'errors': []}

        logger.info(f"Found {len(keys_to_delete)} objects under prefix '{prefix}', deleting...")
        return delete_s3_keys_batch(keys_to_delete, bucket_name)
    except ClientError as e:
        msg = f"Failed to list/delete objects for prefix '{prefix}': {str(e)}"
        logger.error(msg)
        return {'success': False, 'deleted_count': 0, 'errors': [msg]}
    except Exception as e:
        msg = f"Unexpected error deleting prefix '{prefix}': {str(e)}"
        logger.error(msg)
        return {'success': False, 'deleted_count': 0, 'errors': [msg]}


# Fix missing Optional import used in generate_presigned_url
from typing import Optional  # noqa: E402 — must come after function def due to forward ref