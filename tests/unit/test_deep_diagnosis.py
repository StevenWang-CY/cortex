"""Tests for deep bottleneck diagnosis schema extensions."""

from cortex.libs.schemas.intervention import ErrorAnalysis


class TestDeepDiagnosis:
    def test_extended_error_analysis_fields(self):
        ea = ErrorAnalysis(
            error_type="type_mismatch",
            root_cause="Type 'string' is not assignable to type 'number'",
            suggested_fix="Change the return type to string",
            failing_abstraction="getUserAge()",
            symbol_location="src/user.ts:42",
            root_cause_category="type_mismatch",
            minimal_edit="return String(age);",
        )
        assert ea.failing_abstraction == "getUserAge()"
        assert ea.symbol_location == "src/user.ts:42"
        assert ea.root_cause_category == "type_mismatch"
        assert ea.minimal_edit == "return String(age);"

    def test_root_cause_category_values(self):
        for cat in [
            "type_mismatch",
            "null_reference",
            "missing_import",
            "logic_error",
            "api_misuse",
            "concurrency",
            "config",
            "other",
        ]:
            ea = ErrorAnalysis(
                error_type="test", root_cause="test", root_cause_category=cat
            )
            assert ea.root_cause_category == cat

    def test_default_values(self):
        ea = ErrorAnalysis(error_type="syntax", root_cause="missing semicolon")
        assert ea.failing_abstraction == ""
        assert ea.symbol_location == ""
        assert ea.root_cause_category == "other"
        assert ea.minimal_edit == ""
        assert ea.suggested_fix == ""
        assert ea.search_query == ""
        assert ea.relevant_doc_url == ""
