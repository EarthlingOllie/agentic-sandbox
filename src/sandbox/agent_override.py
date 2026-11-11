"""The ``agent-override`` validation step of ``up``.

Before anything is created it checks the plan's agent override: extra services may not
shadow a repo's service, every setting sandbox will override to keep its guarantees is
reported, and a custom image must have ``sh``, ``git`` and ``claude`` on its PATH. The
override itself is rendered by ``sandbox.compose.agent``.
"""

from sandbox.agent_image import REQUIRED_COMMANDS
from sandbox.compose.agent import custom_image, extra_services, overridden_settings
from sandbox.errors import SandboxError
from sandbox.pipeline import UpContext


async def check_agent_override(ctx: UpContext) -> None:
    override = ctx.plan.agent
    if override is None:
        return
    extras = extra_services(override)
    clashes = sorted(set(extras) & set(ctx.services))
    if clashes:
        raise SandboxError(
            f"the agent override adds service(s) {', '.join(clashes)} that a repo already "
            "declares; rename them in the override"
        )
    for note in overridden_settings(override, ctx.set_name, ctx.set_dir, ctx.plan.sources()):
        ctx.warn(f"agent override: ignoring {note}")
    image = custom_image(override)
    if image is not None:
        missing = await ctx.images.missing_commands(image, REQUIRED_COMMANDS)
        if missing:
            raise SandboxError(
                f"agent image {image!r} has no {', '.join(missing)} on its PATH; a custom "
                f"agent image must provide {', '.join(REQUIRED_COMMANDS)}"
            )
    # The extra services run with the set and count towards its state.
    ctx.services = sorted({*ctx.services, *extras})
