class UserFacingError(Exception):
    """Safe to show in Telegram. Must never contain key material."""


class ConfigError(Exception):
    """Missing or invalid configuration. Shown on stderr at startup."""
