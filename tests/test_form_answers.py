from job_agent.services.form_answers import answer_known_application_question


def test_salary_answer_uses_only_profile_expectation() -> None:
    profile = {"candidate": {"compensation": {"monthly_target": 100000, "currency": "RUB"}}}
    assert answer_known_application_question("Укажите вилку зарплатных ожиданий", profile) == "Ориентир по ожиданиям: от 100 000 RUB в месяц."


def test_salary_answer_stops_without_user_expectation() -> None:
    assert answer_known_application_question("Какие зарплатные ожидания?", {"candidate": {}}) is None


def test_salary_range_and_city_use_declared_profile_values() -> None:
    profile = {"candidate": {"compensation": {"monthly_min": 180000, "monthly_target": 200000, "currency": "RUB"}, "location": {"city": "Moscow"}}}
    assert "200 000" in answer_known_application_question("Минимум и комфорт по зарплате", profile)
    assert answer_known_application_question("Укажите город проживания", profile) == "Moscow"
