"""Legacy contribution contracts; approval services belong to the platform host."""
from openclass_core.services.studio_ports import host_service

class LessonContributionError(ValueError):
    pass

class LessonContributionNotFoundError(LessonContributionError):
    pass

class LessonContributionPermissionError(LessonContributionError):
    pass

class LessonContributionConflictError(LessonContributionError):
    pass

def complete_lesson_contribution_merge(*args, **kwargs):
    return host_service('course_contributions', 'complete_lesson_contribution_merge', *args, **kwargs)

def persist_recomputed_contribution_merge(*args, **kwargs):
    return host_service('course_contributions', 'persist_recomputed_contribution_merge', *args, **kwargs)
