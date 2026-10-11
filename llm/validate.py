"""Validator that ensures prose contains only numbers from claims."""

import re
from typing import Union, Optional, Dict, Callable
from core.model import Claim, Culprit
from llm.models import ValidationResult


class NumberValidator:
    """Validates that every number in prose traces back to a claim."""

    def __init__(self, txn_resolver: Optional[Callable[[str], bool]] = None):
        """
        Initialize the validator.

        Args:
            txn_resolver: Optional function(txn_id: str) -> bool that checks if a transaction exists.
        """
        self.txn_resolver = txn_resolver
        self.txn_cache: Dict[str, bool] = {}
        self.violations: list[str] = []

    def validate(
        self,
        prose: Union[str, list[str]],
        claims: list[Claim],
        culprit: Optional[Culprit]
    ) -> ValidationResult:
        """
        Run all three validation checks.

        Returns ValidationResult with passed=True only if all checks pass.
        """
        self.violations = []

        # Normalize prose to a single string
        if isinstance(prose, list):
            prose_str = " ".join(prose)
        else:
            prose_str = prose

        # Run all three checks, collecting all violations
        self._check_numeric_trace(prose_str, claims)
        self._check_evidence_existence(prose_str, claims)
        self._check_culprit_consistency(prose_str, culprit, claims)

        passed = len(self.violations) == 0
        return ValidationResult(
            passed=passed,
            violations=self.violations,
            prose=prose_str
        )

    def _check_numeric_trace(self, prose: str, claims: list[Claim]) -> None:
        """Check 1: Every number must match a claim value."""
        # Strip replay link and URLs before extraction
        cleaned = self._strip_urls(prose)

        # Extract all numbers from the prose
        numbers = self._extract_all_numbers(cleaned)

        for num_str, num_val, context in numbers:
            # Check if this number matches any claim value
            matched = self._match_number_to_claim(num_val, claims)
            if not matched:
                self.violations.append(
                    f"orphan number: '{num_str}' in '{context}' matches no claim"
                )

    def _check_evidence_existence(self, prose: str, claims: list[Claim]) -> None:
        """Check 2: Every evidence ID must resolve via GET /txn/{txn_id}."""
        if not self.txn_resolver:
            return

        # Extract evidence IDs from claims
        for claim in claims:
            for txn_id in claim.evidence:
                if txn_id not in self.txn_cache:
                    exists = self.txn_resolver(txn_id)
                    self.txn_cache[txn_id] = exists

                if not self.txn_cache[txn_id]:
                    self.violations.append(f"unresolvable evidence id: {txn_id}")

    def _check_culprit_consistency(self, prose: str, culprit: Optional[Culprit], claims: list[Claim]) -> None:
        """Check 3: The culprit must be named correctly."""
        if culprit is None:
            # No recurring charge had a positive measured contribution, so
            # there is no "correct" name to enforce here.
            return
        culprit_normalized = culprit.label.lower().strip()
        prose_normalized = prose.lower()

        if culprit_normalized not in prose_normalized:
            self.violations.append(
                f"culprit '{culprit.label}' not found in prose"
            )

    @staticmethod
    def _strip_urls(prose: str) -> str:
        """Remove URLs and replay links from prose."""
        # Remove URLs
        prose = re.sub(r'https?://[^\s]+', '', prose)
        # Remove any token-like patterns (evidence IDs like txn_001)
        prose = re.sub(r'\btxn_[\w]+\b', '', prose, flags=re.IGNORECASE)
        return prose

    @staticmethod
    def _extract_all_numbers(prose: str) -> list[tuple[str, float, str]]:
        """
        Extract all numbers from prose.

        Returns list of (original_string, normalized_value, context).
        Detects: $1,400, $1400, 1400, 27%, 0.27, 1.4k, spelled-out numbers.
        """
        results = []
        seen_positions = set()

        # Pattern for percentages: 27% or 27.4% (check first)
        percent_pattern = r'(\d+\.?\d*)%'
        for match in re.finditer(percent_pattern, prose):
            num_str = match.group()
            normalized = float(match.group(1)) / 100.0
            context = prose[max(0, match.start()-20):min(len(prose), match.end()+20)]
            results.append((num_str, normalized, context))
            seen_positions.add((match.start(), match.end()))

        # Pattern for k suffix: 1.4k or 1.4K (before general currency)
        k_pattern = r'(\d+\.?\d*)\s*[kK]\b'
        for match in re.finditer(k_pattern, prose):
            if (match.start(), match.end()) in seen_positions:
                continue
            num_str = match.group().strip()
            normalized = float(match.group(1)) * 1000
            context = prose[max(0, match.start()-20):min(len(prose), match.end()+20)]
            results.append((num_str, normalized, context))
            seen_positions.add((match.start(), match.end()))

        # Pattern for currency: $XXX or XXX with comma/dots, negative amounts
        currency_pattern = r'-?\$[\d,]+\.?\d*|-?[\d,]+\.\d{2}'
        for match in re.finditer(currency_pattern, prose):
            if (match.start(), match.end()) in seen_positions:
                continue
            num_str = match.group()
            try:
                normalized = float(num_str.replace('$', '').replace(',', ''))
                context = prose[max(0, match.start()-20):min(len(prose), match.end()+20)]
                results.append((num_str, normalized, context))
                seen_positions.add((match.start(), match.end()))
            except ValueError:
                pass

        # Detect spelled-out numbers (flagged as violations)
        spelled_pattern = r'\b(twenty|thirty|forty|fifty|hundred|thousand|million|dozen|half)\b'
        for match in re.finditer(spelled_pattern, prose, re.IGNORECASE):
            if (match.start(), match.end()) not in seen_positions:
                num_str = match.group()
                context = prose[max(0, match.start()-20):min(len(prose), match.end()+20)]
                results.append((num_str, -1, context))  # -1 flags as violation
                seen_positions.add((match.start(), match.end()))

        return results

    @staticmethod
    def _match_number_to_claim(num_val: float, claims: list[Claim]) -> bool:
        """Check if a number matches exactly one claim value within tolerance."""
        if num_val == -1:  # spelled-out number
            return False

        if not claims:
            return False

        for claim in claims:
            claim_val = claim.value
            # culprit_label's value is the merchant name (core.model.Claim.value
            # is `float | str` by design) - no numeric claim to match against.
            if not isinstance(claim_val, (int, float)):
                continue

            # Probability: claim is 0-1, prose might be 0-1 or 0-100. Tolerance
            # is 0.005 (half a percentage point) so a claim like 0.362 written
            # as "36%" still matches after rounding to the nearest whole percent
            # (the render prompt instructs exactly this rounding).
            if claim.unit == "probability":
                if abs(num_val - claim_val) < 0.005:
                    return True
                if claim_val < 1.0 and 0 < num_val < 100:
                    if abs(num_val / 100.0 - claim_val) < 0.005:
                        return True
                continue

            # Dollar amounts: accept 2 decimal place variance
            if claim.unit == "usd":
                if abs(num_val - claim_val) < 0.01:
                    return True
                continue

            # Default (week, count, or unrecognized unit): exact match with
            # small tolerance
            if abs(num_val - claim_val) < 0.001:
                return True

        return False


def validate(
    prose: Union[str, list[str]],
    claims: list[Claim],
    culprit: Optional[Culprit],
    txn_resolver: Optional[Callable[[str], bool]] = None
) -> ValidationResult:
    """
    Validate that prose contains only traceable numbers and references.

    Args:
        prose: The prose to validate (string or list of strings)
        claims: The claim list that's the only source of truth for numbers
        culprit: The culprit that should be named in the prose
        txn_resolver: Optional function to check if transaction IDs exist

    Returns:
        ValidationResult with passed=True if all checks pass
    """
    validator = NumberValidator(txn_resolver)
    return validator.validate(prose, claims, culprit)
