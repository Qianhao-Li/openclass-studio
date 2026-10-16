from typing import Any, Protocol

class ModelBillingError(RuntimeError):

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

class ModelBillingPort(Protocol):

    def reserve_model_call(self, **kwargs) -> Any:
        ...

    def settle_model_call(self, **kwargs) -> Any:
        ...

    def release_model_call(self, **kwargs) -> Any:
        ...

    def record_openrouter_usage(self, **kwargs) -> Any:
        ...

class PlatformModelAccessPort(Protocol):
    config: Any

    def api_key_for_user(self, owner_user_id: str) -> str:
        ...
