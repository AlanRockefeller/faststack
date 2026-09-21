"""Platform-independent drag completion helpers."""


def wayland_ignore_action_completes(
    payload_read: bool, accepted_action_seen: bool
) -> bool:
    """Return whether an unreliable Wayland IgnoreAction represents a drop."""
    return payload_read and accepted_action_seen
