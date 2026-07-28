"""Perceptual hashing (dHash) for near-duplicate detection.

dHash compares each pixel to its right-hand neighbour on a 9x8 greyscale
reduction: 64 gradient bits that survive re-encoding, scaling, and mild
colour grading — exactly the transformations a proxy pipeline applies.
Implemented on Pillow alone so no extra dependency enters the image.
"""

from pathlib import Path

HASH_SIZE = 8


def dhash(path: Path) -> str | None:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - media extra always installed
        return None
    try:
        with Image.open(path) as img:
            small = img.convert("L").resize((HASH_SIZE + 1, HASH_SIZE), Image.Resampling.LANCZOS)
            pixels = list(small.getdata())
    except Exception:
        return None  # never trust file contents

    bits = 0
    for row in range(HASH_SIZE):
        offset = row * (HASH_SIZE + 1)
        for col in range(HASH_SIZE):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def hamming_distance(a: str, b: str) -> int:
    """Bit difference between two hashes; < 6 is visually near-identical."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")
