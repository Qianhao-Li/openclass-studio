from __future__ import annotations
import os
LEGACY_STRUCTURED_MODE = 'legacy_structured'
NATIVE_PI_MODE = 'native_pi'

def teaching_agent_mode() -> str:
    """Return the configured blank-board teaching workflow.

    The structured Requirement Manager -> BoardEditor workflow is the stable
    default. Native Pi remains available as an explicit opt-in while its
    workflow integration is evaluated.
    """
    configured = os.getenv('OPENCLASS_TEACHING_AGENT_MODE', LEGACY_STRUCTURED_MODE).strip().lower()
    if configured in {'native', 'native-pi', NATIVE_PI_MODE}:
        return NATIVE_PI_MODE
    return LEGACY_STRUCTURED_MODE

def native_pi_teaching_enabled() -> bool:
    return teaching_agent_mode() == NATIVE_PI_MODE
