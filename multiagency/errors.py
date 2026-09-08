"""Errors that should stop the process rather than degrade quietly."""


class ConfigError(Exception):
    """The content YAML is unusable. Raised on load, never swallowed."""


class HermesError(Exception):
    """Hermes was unreachable or returned something outside the contract."""


class PublishError(Exception):
    """The post was not accepted by the platform."""


class ImageError(Exception):
    """The image could not be rendered. Never fatal to the post it belonged to."""
