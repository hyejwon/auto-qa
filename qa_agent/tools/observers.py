# qa_agent/tools/observers.py
import base64, asyncio, logging
import numpy as np
from typing import Any, Dict, Optional
from PIL import Image
from io import BytesIO
from .sanitizer import normalize_tool_output

logger = logging.getLogger(__name__)

def _calculate_image_similarity(b64_img1: str, b64_img2: str) -> float:
    """
    Calculate structural similarity between two base64 encoded images.
    Returns similarity score (0.0 to 1.0).
    Uses SSIM if available, falls back to pixel difference ratio.
    """
    try:
        # Decode base64 to PIL Images
        img1_data = base64.b64decode(b64_img1)
        img2_data = base64.b64decode(b64_img2)

        img1 = Image.open(BytesIO(img1_data)).convert('RGB')
        img2 = Image.open(BytesIO(img2_data)).convert('RGB')

        # Resize to same dimensions if needed
        if img1.size != img2.size:
            img2 = img2.resize(img1.size, Image.LANCZOS)

        # Convert to numpy arrays
        arr1 = np.array(img1)
        arr2 = np.array(img2)

        # Try to use SSIM from skimage
        try:
            from skimage.metrics import structural_similarity as ssim
            # Calculate SSIM for each channel and average
            similarity = ssim(arr1, arr2, channel_axis=2)
            logger.debug(f"   📊 Image similarity (SSIM): {similarity:.4f}")
            return similarity
        except ImportError:
            # Fallback: simple pixel difference ratio
            diff = np.abs(arr1.astype(float) - arr2.astype(float))
            diff_ratio = np.mean(diff) / 255.0  # Normalize to 0-1
            similarity = 1.0 - diff_ratio
            logger.debug(f"   📊 Image similarity (pixel diff): {similarity:.4f}")
            return similarity
    except Exception:
        # On any error, return 0 (images are different)
        return 0.0

async def wait_for_stable_screen(
    tool_map,
    stable_required: int = 2,
    max_tries: int = 8,
    interval: float = 0.6,
    similarity_threshold: float = 0.95
) -> dict:
    """
    Wait for screen to stabilize by comparing screenshots.

    Args:
        tool_map: Dictionary of available tools
        stable_required: Number of consecutive similar screenshots required
        max_tries: Maximum number of attempts
        interval: Time to wait between screenshots (seconds)
        similarity_threshold: Minimum similarity score (0.0-1.0) to consider images same

    Returns:
        Dict with status, stable flag, tries count, and last screenshot
    """
    take = tool_map.get("take_screenshot")
    if not take:
        return {"status": "error", "reason": "take_screenshot tool not found"}

    last_b64: Optional[str] = None
    stable_count = 0
    last_img = None

    for i in range(max_tries):
        out = await take.ainvoke({"save_debug": True}) if hasattr(take, "ainvoke") else take.invoke({"save_debug": True})
        parsed = normalize_tool_output(out)
        b64 = parsed.get("image", "")
        if not b64:
            await asyncio.sleep(interval)
            continue

        # Compare with previous screenshot
        if last_b64 is not None:
            similarity = _calculate_image_similarity(last_b64, b64)
            logger.debug(f"   🔍 Attempt {i + 1}: similarity={similarity:.4f}, threshold={similarity_threshold:.2f}, stable_count={stable_count}")
            if similarity >= similarity_threshold:
                stable_count += 1
                logger.debug(f"      ✓ Images similar (stable_count={stable_count}/{stable_required})")
            else:
                logger.debug(f"      ✗ Images differ, resetting stable_count")
                stable_count = 0

        last_b64 = b64
        last_img = parsed

        if stable_count >= stable_required:
            return {"status": "success", "stable": True, "tries": i + 1, "last": parsed}

        await asyncio.sleep(interval)

    return {"status": "success", "stable": False, "tries": max_tries, "last": last_img}
