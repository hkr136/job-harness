from job_agent.llm import prompts


def test_user_prompt_override_and_reset(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(prompts, "USER_HOME", tmp_path)
    monkeypatch.setattr("job_agent.config.settings.USER_HOME", tmp_path)

    prompts.save_system_prompt("writing", "Пиши строго и кратко.")
    assert prompts.get_system_prompt("writing") == "Пиши строго и кратко."

    prompts.reset_system_prompt("writing")
    assert prompts.get_system_prompt("writing") == prompts.DEFAULT_PROMPTS["writing"]
