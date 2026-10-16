import pytest
from openclass_core.models import AIModelCatalog, AIModelOption, AIModelSelection
from openclass_core.services.studio_personal_models import resolve_personal_model_selection


def catalog():
    return AIModelCatalog(text=[AIModelOption(provider="deepseek", model="test-personal-model", access_method="personal_api",
        label="Personal model", capability="text", enabled=True, configured=True)],
        defaults={"text": AIModelSelection(provider="deepseek", model="test-personal-model", access_method="personal_api")})


def test_personal_model_selection_never_falls_back_to_platform_credits():
    personal = AIModelSelection(provider="deepseek", model="test-personal-model", access_method="personal_api")
    assert resolve_personal_model_selection(personal, user_id="local_one", catalog_builder=lambda _:catalog()) == personal
    with pytest.raises(RuntimeError, match="unavailable"):
        resolve_personal_model_selection(personal.model_copy(update={"access_method":"platform_credits"}),
            user_id="local_one", catalog_builder=lambda _:catalog())


def test_unavailable_explicit_model_is_preserved_as_an_error():
    chosen = AIModelSelection(provider="deepseek", model="missing-personal-model", access_method="personal_api")
    with pytest.raises(RuntimeError, match="unavailable"):
        resolve_personal_model_selection(chosen, user_id="local_one", catalog_builder=lambda _:catalog())
