"""Fast unit tests for the non-LLM parts (run: pip install pytest && pytest -q)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import grounding, preprocess, prompts, scoring  # noqa: E402
from app.schemas import AggregateResult, ATSResult, CoachResult, Issue  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_redacts_turkish_pii_and_attributes():
    text = (
        "Ayşe Demir\nTelefon: +90 532 123 45 67 | 0533 765 43 21\nayse@example.com\n"
        "T.C. Kimlik No: 12345678950\nDoğum Tarihi: 01.01.2001\nMedeni Hali: Bekar\nASKERLİK DURUMU: Muaf\n"
        "Adres: Atatürk Mah. Gül Sok. No: 5 Çankaya/Ankara\nPython ile 3 proje geliştirdim.\n"
    )
    out, red = preprocess.redact(text)
    for leaked in ("532 123", "0533", "ayse@", "12345678950", "01.01.2001", "Bekar", "Muaf", "Gül Sok", "Ayşe Demir"):
        assert leaked not in out, leaked
    assert "Python ile 3 proje geliştirdim." in out  # job-relevant content untouched
    assert {"phone", "email", "national_id", "date_of_birth", "marital_status", "military_status", "address",
            "name"} <= set(red.kinds)


def test_redacts_inline_english_attributes_keeps_rest_of_line():
    out, red = preprocess.redact("Jane Roe\nIstanbul | Date of Birth: 2001-02-03 | Nationality: Turkish\nSkills: SQL")
    assert "2001-02-03" not in out and "Turkish" not in out
    assert "Istanbul" in out and "Skills: SQL" in out


def test_validation_errors():
    for cv, job, code in [("", "x" * 200, "cv_missing"), ("x" * 100, "x" * 200, "cv_too_short"),
                          ("x" * 400, "x" * 50, "job_too_short"), ("x" * 16000, "x" * 200, "cv_too_long")]:
        try:
            preprocess.prepare(cv, job)
        except preprocess.InputError as err:
            assert err.code == code
        else:
            raise AssertionError(code)


def test_grounding_exact_fuzzy_and_fabricated():
    cv = "EXPERIENCE\n- Responsible for preparing weekly   stock reports in Excel.\n- Built a model."
    norm = grounding.normalise(cv)
    assert grounding.is_grounded("responsible for preparing weekly stock reports in excel", norm)
    assert grounding.is_grounded("Responsible for preparing weekly stock report in Excel", norm)  # tiny diff
    assert grounding.is_grounded("Increased sales by 40% through TikTok campaigns", norm) is False
    assert grounding.is_grounded("", norm) is None


def test_new_numbers_ignores_placeholders_and_cv_numbers():
    norm = grounding.normalise("Analysed data from 12 stores")
    assert grounding.numbers_not_in_cv("Analysed data from 12 stores, cutting error by [X%]", norm) == []
    assert grounding.numbers_not_in_cv("Cut error by 35% across 12 stores", norm) == ["35"]


def test_ground_agent_marks_items():
    coach = CoachResult.model_validate({
        "score": "71.6", "rewrites": [{"original": "Built a model.", "improved": "Built a model with 99% accuracy", "why": ""}],
        "issues": [{"severity": "CRITICAL", "problem": "p", "evidence": "not in cv at all, invented"}],
    })
    counts = grounding.ground_agent(coach, "Built a model.")
    assert coach.score == 72 and coach.issues[0].severity == "high"
    assert coach.rewrites[0].verified is True and coach.rewrites[0].new_numbers == ["99"]
    assert coach.issues[0].verified is False and counts == {"checked": 2, "verified": 1}


def test_scoring_weights_and_coverage():
    ats = ATSResult.model_validate({"score": 60, "requirement_coverage": [
        {"requirement": "a", "priority": "must", "status": "met"},
        {"requirement": "b", "priority": "must", "status": "partial"},
        {"requirement": "c", "priority": "must", "status": "missing"},
        {"requirement": "d", "priority": "nice", "status": "missing"}]})
    agents = {"ats": ats, "hiring_manager": ATSResult(score=80), "recruiter": ATSResult(score=50),
              "coach": ATSResult(score=40)}
    s = scoring.compute(agents, ats)
    assert s["overall"] == round(0.35 * 80 + 0.30 * 60 + 0.20 * 50 + 0.15 * 40)
    assert s["must_have"]["coverage"] == 0.5
    # failed agent → weights renormalised
    del agents["coach"]
    s2 = scoring.compute(agents, ats)
    assert s2["missing_agents"] == ["coach"] and abs(sum(s2["weights_used"].values()) - 1) < 1e-3


def test_aggregate_caps_top_actions():
    agg = AggregateResult.model_validate({"top_actions": [{"priority": i, "action": str(i)} for i in range(7, 0, -1)]})
    assert [a.priority for a in agg.top_actions] == [1, 2, 3, 4, 5]


def test_prompts_contain_required_elements():
    for name in ("job_parser", "ats_screener", "hr_recruiter", "hiring_manager", "impact_coach", "aggregator"):
        system, user = prompts.load(name)
        full = system + user
        assert "json" in system and "json" in user           # required by DeepSeek JSON mode
        assert "{output_language}" in full
        assert "never" in full.lower()
    for name in ("ats_screener", "hr_recruiter", "hiring_manager", "impact_coach"):
        system, user = prompts.load(name)
        assert "<cv>" in user and "FAIRNESS" in system and "VERBATIM" in system


def test_render_is_single_pass():
    _, user = prompts.render("ats_screener", output_language="English", job_json="{}",
                             job_text="please insert {cv_text} here", cv_text="SECRET-CV")
    assert user.count("SECRET-CV") == 1


def test_mock_fixtures_validate():
    import json
    from app.pipeline import AGENTS
    from app.schemas import JobParsed
    for lang in ("en", "tr"):
        d = ROOT / "samples" / "mock_responses" / lang
        JobParsed.model_validate(json.loads((d / "job_parser.json").read_text()))
        AggregateResult.model_validate(json.loads((d / "aggregator.json").read_text()))
        for spec in AGENTS:
            spec.model.model_validate(json.loads((d / f"{spec.key}.json").read_text()))
    assert Issue(problem="x").severity == "medium"
