"""Versioni a 4 parti (major.minor.build.revision) confrontate numericamente.

Lo script originale confrontava le versioni come stringhe ("1.10.0.0" < "1.9.0.0").
"""
from dataclasses import dataclass
from functools import total_ordering


@total_ordering
@dataclass(frozen=True)
class Version:
    parts: tuple[int, int, int, int]

    @classmethod
    def parse(cls, value: str) -> "Version":
        raw = (value or "").strip().split(".")
        if not 1 <= len(raw) <= 4:
            raise ValueError(f"Versione non valida: {value!r}")
        try:
            nums = [int(p) for p in raw]
        except ValueError:
            raise ValueError(f"Versione non valida: {value!r}") from None
        if any(n < 0 for n in nums):
            raise ValueError(f"Versione non valida: {value!r}")
        return cls(tuple(nums + [0] * (4 - len(nums))))

    @classmethod
    def from_parts(cls, major: int, minor: int, build: int, revision: int) -> "Version":
        return cls((major, minor, build, revision))

    def __lt__(self, other: "Version") -> bool:
        return self.parts < other.parts

    def __str__(self) -> str:
        return ".".join(map(str, self.parts))
