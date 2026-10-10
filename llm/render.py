"""Render typed claims into natural prose using Gemini."""

from typing import Union, Optional
import os
import json

from core.model import Claim, Culprit


def render(
    claims: list[Claim],
    culprit: Culprit,
    violation_feedback: Optional[list[str]] = None
) -> str:
    """
    Render claims to natural prose using Gemini.

    Type signature enforces that render receives ONLY claims and culprit,
    never raw SimResult or transaction records.

    Args:
        claims: Typed claims (only source of truth for numbers)
        culprit: Culprit to name in prose
        violation_feedback: Optional violations from previous attempt (for retry)

    Returns:
        Natural prose (string or list[str] per Blocker 1 agreement)
    """
    try:
        return _render_with_gemini(claims, culprit, violation_feedback)
    except Exception:
        # Graceful fallback
        return _render_fallback(claims, culprit)


def _render_with_gemini(
    claims: list[Claim],
    culprit: Culprit,
    violation_feedback: Optional[list[str]] = None
) -> str:
    """Render using Gemini API."""
    try:
        import google.generativeai as genai
    except ImportError:
        return _render_fallback(claims, culprit)

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return _render_fallback(claims, culprit)

    genai.configure(api_key=api_key)

    # Build claim summary for the prompt
    claim_summary = _build_claim_summary(claims)

    feedback_section = ""
    if violation_feedback:
        feedback_section = f"\n\nPrevious attempt had these violations—fix them:\n"
        for violation in violation_feedback:
            feedback_section += f"- {violation}\n"

    prompt = f"""You are helping a user understand their spending analysis. Write a short, natural text message about their spending decision.

Culprit (merchant/category): {culprit.label}

Claims (the ONLY source of numbers in your response):
{claim_summary}

CRITICAL RULES:
1. Every number in your response MUST come from a claim's value field, verbatim.
2. Do not compute, round, average, or infer any figure.
3. Write numbers as digits, never spelled out.
4. NEVER hedge numerically: no "about", "around", "roughly", "~", "nearly", "almost", "over", "under" before numbers.
5. Format probabilities as whole-number percents: 0.27 → "27%" (no "about").
6. Name the culprit as: {culprit.label}
7. Do not reference transactions or IDs unless a claim mentions them.
8. No markdown, emoji, or bullet points. It's a text message.

Output format:
- 3–5 sentences, iMessage-appropriate
- Lead with probability if available
- Name the culprit
- End with a reference to reviewing the replay/details{feedback_section}

Generate the prose now:"""

    model_name = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(prompt)

    prose = response.text.strip()
    # Remove markdown if any
    prose = prose.replace("```", "").replace("**", "").replace("*", "")

    return prose


def _render_fallback(claims: list[Claim], culprit: Culprit) -> str:
    """Simple fallback rendering when Gemini unavailable."""
    if not claims:
        return f"I analyzed your spending around {culprit.label}. Review the details in the replay."

    parts = []

    # Extract key numbers
    prob_claim = next((c for c in claims if 'prob' in c.kind.lower()), None)
    amount_claim = next((c for c in claims if 'amount' in c.kind.lower() or 'cost' in c.kind.lower()), None)

    # Lead with probability
    if prob_claim:
        prob_pct = int(round(prob_claim.value * 100))
        parts.append(f"There's a {prob_pct}% chance you can afford this.")

    # Name the culprit and add amount
    if amount_claim:
        amount_str = _format_dollar(amount_claim.value)
        parts.append(f"{culprit.label} is {amount_str}.")
    else:
        parts.append(f"I analyzed {culprit.label} in your spending.")

    # Add other relevant info
    for claim in claims:
        if claim.kind.lower() not in ['probability', 'amount', 'cost']:
            if 'risk' in claim.kind.lower():
                val = _format_value(claim.value, claim.kind)
                parts.append(f"Your risk is {val}.")

    parts.append("Check the replay for more details.")

    return " ".join(parts)


def _build_claim_summary(claims: list[Claim]) -> str:
    """Build a readable summary of claims for the prompt."""
    if not claims:
        return "(no claims)"

    lines = []
    for claim in claims:
        formatted_value = _format_value(claim.value, claim.kind)
        lines.append(f"- {claim.kind}: {formatted_value}")

    return "\n".join(lines)


def _format_dollar(value: float) -> str:
    """Format a float as a dollar amount."""
    if value == int(value):
        return f"${int(value)}"
    return f"${value:.2f}"


def _format_value(value: float, kind: str) -> str:
    """Format a value based on its kind."""
    kind_lower = kind.lower()

    if 'percent' in kind_lower or 'probability' in kind_lower or 'prob' in kind_lower:
        pct = int(round(value * 100))
        return f"{pct}%"
    elif 'dollar' in kind_lower or 'amount' in kind_lower or 'cost' in kind_lower:
        return _format_dollar(value)
    elif value == int(value):
        return str(int(value))
    else:
        return f"{value:.2f}"
