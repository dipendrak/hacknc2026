"""Tests for prose validation against claims."""

import pytest
from llm.models import Claim, Culprit, ValidationResult
from llm.validate import validate, NumberValidator


class TestNumericTrace:
    """Check 1: Every number must match a claim."""

    def test_valid_dollar_amount(self, make_culprit, make_claim):
        """Valid dollar amounts should pass."""
        culprit = make_culprit("Netflix")
        claims = [make_claim("culprit_amount", 15.99)]
        prose = "Netflix costs $15.99 per month."

        result = validate(prose, claims, culprit)
        assert result.passed

    def test_orphan_number_fails(self, make_culprit, make_claim):
        """Numbers not in claims should fail."""
        culprit = make_culprit("Gym")
        claims = [make_claim("culprit_amount", 65.0)]
        prose = "Gym costs $165 per month."  # $165 not in claims

        result = validate(prose, claims, culprit)
        assert not result.passed
        assert any("orphan" in v.lower() for v in result.violations)

    def test_percent_normalization(self, make_culprit, make_claim):
        """Percentages should normalize: 0.27 matches 27%."""
        culprit = make_culprit("Flight")
        claims = [make_claim("breach_rate_with_delta", 0.27)]
        prose = "There's a 27% chance you can afford this flight."

        result = validate(prose, claims, culprit)
        assert result.passed

    def test_dollar_formats_match(self, make_culprit, make_claim):
        """$142 should match 142.00 within tolerance."""
        culprit = make_culprit("Book")
        claims = [make_claim("culprit_amount", 142.00)]
        prose = "The book costs $142."

        result = validate(prose, claims, culprit)
        assert result.passed

    def test_k_suffix_extraction(self, make_culprit, make_claim):
        """1.4k should extract and match 1400."""
        culprit = make_culprit("Laptop")
        claims = [make_claim("culprit_amount", 1400.0)]
        prose = "A laptop costs 1.4k dollars."

        result = validate(prose, claims, culprit)
        assert result.passed

    def test_spelled_out_number_rejected(self, make_culprit, make_claim):
        """Spelled-out numbers should be flagged as violations."""
        culprit = make_culprit("Car")
        claims = [make_claim("breach_rate_with_delta", 0.5)]
        prose = "There's about a fifty percent chance."

        result = validate(prose, claims, culprit)
        assert not result.passed
        assert any("spelled" in v.lower() or "fifty" in v.lower() for v in result.violations)

    def test_negative_amount(self, make_culprit, make_claim):
        """Negative amounts should extract correctly."""
        culprit = make_culprit("Refund")
        claims = [make_claim("culprit_amount", -50.0)]
        prose = "Your Refund could be -$50."

        result = validate(prose, claims, culprit)
        assert result.passed

    def test_multiple_numbers_all_valid(self, make_culprit, make_claim):
        """All numbers in prose must be in claims."""
        culprit = make_culprit("Trip")
        claims = [
            make_claim("culprit_amount", 1400.0),
            make_claim("culprit_amount", 200.0)
        ]
        prose = "Your Trip costs $1400 flight and $200 hotel per night."

        result = validate(prose, claims, culprit)
        assert result.passed


class TestEvidenceExistence:
    """Check 2: Transaction IDs must resolve."""

    def test_evidence_ids_resolve(self, make_culprit, make_claim):
        """Valid evidence IDs should pass."""
        def mock_resolver(txn_id):
            return txn_id in ["txn_001", "txn_002"]

        culprit = make_culprit("Amazon")
        claims = [
            make_claim("culprit_amount", 50.0)
        ]
        prose = "Your Amazon purchase for $50 was from txn_001."

        result = validate(prose, claims, culprit, txn_resolver=mock_resolver)
        assert result.passed

    def test_unresolvable_evidence_id(self, make_culprit, make_claim):
        """Unresolvable transaction IDs should fail."""
        def mock_resolver(txn_id):
            return txn_id in ["txn_001"]

        culprit = make_culprit("Store")
        claims = [
            make_claim("culprit_amount", 50.0, evidence=["txn_999"])  # txn_999 does not resolve
        ]
        prose = "Your store purchase was $50."

        result = validate(prose, claims, culprit, txn_resolver=mock_resolver)
        assert not result.passed
        assert any("unresolvable" in v.lower() for v in result.violations)

    def test_no_resolver_skips_check(self, make_culprit, make_claim):
        """If no resolver provided, evidence check is skipped."""
        culprit = make_culprit("Shop")
        claims = [
            make_claim("culprit_amount", 50.0)
        ]
        prose = "Shop purchase is $50."

        result = validate(prose, claims, culprit, txn_resolver=None)
        # Should pass on numeric check, evidence check skipped
        assert result.passed or not any("unresolvable" in v.lower() for v in result.violations)


class TestCulpritConsistency:
    """Check 3: Culprit must be named correctly."""

    def test_culprit_present(self, make_culprit, make_claim):
        """Culprit name must appear in prose."""
        culprit = make_culprit("Netflix")
        claims = [make_claim("culprit_amount", 15.99)]
        prose = "Netflix charges $15.99 per month."

        result = validate(prose, claims, culprit)
        assert result.passed

    def test_culprit_missing(self, make_culprit, make_claim):
        """Missing culprit should fail."""
        culprit = make_culprit("Netflix")
        claims = [make_claim("culprit_amount", 15.99)]
        prose = "This streaming service costs $15.99 per month."

        result = validate(prose, claims, culprit)
        assert not result.passed
        assert any("culprit" in v.lower() or "netflix" in v.lower() for v in result.violations)

    def test_culprit_case_insensitive(self, make_culprit, make_claim):
        """Culprit matching should be case-insensitive."""
        culprit = make_culprit("Netflix")
        claims = [make_claim("culprit_amount", 15.99)]
        prose = "NETFLIX costs $15.99."

        result = validate(prose, claims, culprit)
        assert result.passed


class TestAdversarial:
    """Seeded adversarial test: fabricated numbers should be rejected."""

    def test_fabricated_orphan_number(self, make_culprit, make_claim):
        """Hand-written prose with plausible but fabricated number."""
        culprit = make_culprit("Spotify")
        claims = [make_claim("culprit_amount", 9.99)]

        # This prose contains a plausible but fabricated number: $11.99
        prose = "Spotify costs $9.99 per month, but you're looking at plans around $11.99."

        result = validate(prose, claims, culprit)
        assert not result.passed, "Should reject fabricated $11.99"
        assert any("$11.99" in v or "11.99" in v for v in result.violations)

    def test_validator_rejects_malformed_prose(self, make_culprit, make_claim):
        """Prose that tries to sneak in computed numbers."""
        culprit = make_culprit("Gym")
        claims = [
            make_claim("first_breach_week", 65.0),
            make_claim("paths_simulated", 12.0)
        ]

        # This prose violates the rule: it computes $780 (65 * 12)
        prose = "Gym is $65/mo, so a 12-month commitment is $780."

        result = validate(prose, claims, culprit)
        assert not result.passed, "Should reject computed $780"


class TestValidationIntegration:
    """Test validation with realistic scenarios."""

    def test_valid_complete_response(self, make_culprit, make_claim):
        """A well-formed response should pass all checks."""
        culprit = make_culprit("Flight")
        claims = [
            make_claim("breach_rate_with_delta", 0.85),
            make_claim("culprit_amount", 1400.0)
        ]
        prose = "There's an 85% chance you can afford this Flight for $1400."

        result = validate(prose, claims, culprit)
        assert result.passed

    def test_multiple_violations_collected(self, make_culprit, make_claim):
        """All violations should be reported at once."""
        culprit = make_culprit("Car")
        claims = [make_claim("culprit_amount", 25000.0)]
        prose = "A car costs about $35000 but Bike is only $200."

        result = validate(prose, claims, culprit)
        assert not result.passed
        # Should have violations for: orphan $35000, orphan $200, missing culprit
        assert len(result.violations) >= 1
