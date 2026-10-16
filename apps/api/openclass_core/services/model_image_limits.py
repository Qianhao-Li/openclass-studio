"""Image limits of the selected transfer channel, separate from text context."""
from dataclasses import dataclass
MiB = 1024 * 1024

@dataclass(frozen=True)
class ImageTransferLimits:
    per_image: int = 20 * MiB
    total_images: int = 40 * MiB
    request_bytes: int = 64 * MiB
    count: int = 6

def image_transfer_limits(provider, model):
    if provider == 'deepseek':
        from openclass_core.services.deepseek_api import canonical_deepseek_model
        if canonical_deepseek_model(model) == 'deepseek-flash':
            return ImageTransferLimits(32 * MiB, 64 * MiB, 48 * MiB, 600)
    return ImageTransferLimits()
