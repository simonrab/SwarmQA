"""Screenshot preparation: downscale large PNGs to bound image tokens.

The model sees `width` x `height` pixels. `to_points` maps a coordinate in
that image back to the driver's point space using the original size and
the observation's `scale` (pixels per point).
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from swarmqa.llm.protocol import ModelError

DEFAULT_MAX_EDGE = 1568


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    media_type: str
    width: int
    height: int
    original_width: int
    original_height: int
    scale: float = 1.0

    @property
    def b64(self) -> str:
        return base64.standard_b64encode(self.data).decode("ascii")

    @property
    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.b64}"

    @property
    def estimated_tokens(self) -> int:
        """Rough image token count (width x height / 750), for budget estimates."""
        return max(1, (self.width * self.height) // 750)

    def to_points(self, x: float, y: float) -> tuple[float, float]:
        factor_x = self.original_width / self.width if self.width else 1.0
        factor_y = self.original_height / self.height if self.height else 1.0
        scale = self.scale or 1.0
        return (round(x * factor_x / scale, 1), round(y * factor_y / scale, 1))


def prepare_image(path: Path | str, *, max_edge: int = DEFAULT_MAX_EDGE, scale: float = 1.0) -> PreparedImage:
    """Load a screenshot, downscale so the long edge is at most `max_edge`, re-encode as PNG.

    Raise `ModelError` when the file is missing or not an image.
    """
    source = Path(path)
    try:
        with Image.open(source) as img:
            img.load()
            original = img.size
            work = img
            if work.mode not in ("RGB", "RGBA", "L"):
                work = work.convert("RGBA")
            long_edge = max(original)
            if max_edge > 0 and long_edge > max_edge:
                ratio = max_edge / long_edge
                size = (max(1, round(original[0] * ratio)), max(1, round(original[1] * ratio)))
                work = work.resize(size, Image.LANCZOS)
            if work is img and img.format == "PNG":
                data = source.read_bytes()
            else:
                buffer = io.BytesIO()
                work.save(buffer, format="PNG", optimize=True)
                data = buffer.getvalue()
            width, height = work.size
    except FileNotFoundError as exc:
        raise ModelError(f"screenshot is missing: {source}") from exc
    except OSError as exc:
        raise ModelError(f"screenshot is unreadable: {exc}") from exc
    return PreparedImage(
        data=data,
        media_type="image/png",
        width=width,
        height=height,
        original_width=original[0],
        original_height=original[1],
        scale=scale,
    )
