"""In-memory stand-in for the agent image probe (``sandbox.agent_image.ImageProbe``)."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from sandbox.agent_image import ImageError


@dataclass
class FakeImageProbe:
    # image -> the commands it has on PATH; an image not listed here can't be run at all.
    images: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    probed: list[str] = field(default_factory=list[str])

    async def missing_commands(self, image: str, commands: Sequence[str]) -> list[str]:
        self.probed.append(image)
        if image not in self.images:
            raise ImageError(f"cannot run agent image {image!r}: pull access denied")
        return [command for command in commands if command not in self.images[image]]
