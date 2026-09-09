"""Provider selection, driven entirely by Settings.STORAGE_PROVIDER.
Every provider returned here is wrapped in InstrumentedStorageProvider —
metrics, audit logging, and tenant-scope assertion happen exactly once,
regardless of which cloud provider is configured."""
from functools import lru_cache

from app.config import settings
from app.storage.base import StorageProvider
from app.storage.instrumented import InstrumentedStorageProvider


@lru_cache
def get_storage_provider() -> StorageProvider:
    provider_name = settings.STORAGE_PROVIDER

    if provider_name == "s3":
        from app.storage.providers.s3 import S3StorageProvider
        raw = S3StorageProvider(
            bucket=settings.S3_BUCKET, region=settings.S3_REGION,
            endpoint_url=settings.S3_ENDPOINT_URL, kms_key_id=settings.AWS_KMS_KEY_ID,
        )
    elif provider_name == "gcs":
        from app.storage.providers.gcs import GCSStorageProvider
        raw = GCSStorageProvider(
            bucket_name=settings.GCS_BUCKET, credentials_path=settings.GCS_CREDENTIALS_PATH,
            kms_key_name=settings.GCS_KMS_KEY_NAME,
        )
    elif provider_name == "azure":
        from app.storage.providers.azure_blob import AzureBlobStorageProvider
        raw = AzureBlobStorageProvider(
            account_url=settings.AZURE_ACCOUNT_URL, container=settings.AZURE_CONTAINER,
            account_key=settings.AZURE_ACCOUNT_KEY,
        )
    elif provider_name == "cloudinary":
        from app.storage.providers.cloudinary_provider import CloudinaryStorageProvider
        raw = CloudinaryStorageProvider(
            cloud_name=settings.CLOUDINARY_CLOUD_NAME, api_key=settings.CLOUDINARY_API_KEY,
            api_secret=settings.CLOUDINARY_API_SECRET,
        )
    elif provider_name == "local":
        if settings.ENV == "production":
            raise RuntimeError("STORAGE_PROVIDER=local is not permitted when ENV=production")
        from app.storage.providers.local import LocalFilesystemStorageProvider
        raw = LocalFilesystemStorageProvider(root_dir=settings.LOCAL_STORAGE_ROOT)
    else:
        raise RuntimeError(f"Unknown STORAGE_PROVIDER: {provider_name!r}")

    return InstrumentedStorageProvider(raw)


def reset_storage_provider_cache() -> None:
    """Test-only helper — clears the lru_cache so tests can swap
    STORAGE_PROVIDER between cases without process restart."""
    get_storage_provider.cache_clear()
