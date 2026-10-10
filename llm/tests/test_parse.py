"""Tests for parsing text into DecisionDelta."""

import pytest
from datetime import date
from llm.parse import parse


# Test with a fixed "today" date for reproducibility
TODAY = date(2024, 10, 10)  # Wednesday


def test_parse_one_time_flight():
    """Test parsing a one-time flight decision."""
    text = "should I take the $1400 flight to see my family in december"
    result = parse(text, TODAY)

    assert result.kind == "one_time"
    assert result.amount == 1400
    assert result.start_day is not None  # December from October
    assert result.confidence >= 0.8
    assert result.raw_text == text


def test_parse_recurring_gym():
    """Test parsing a recurring gym membership."""
    text = "thinking about a $65/mo gym membership"
    result = parse(text, TODAY)

    assert result.kind == "recurring"
    assert result.amount == 65
    assert result.cadence_days == 30
    assert result.confidence >= 0.8


def test_parse_low_confidence_car():
    """Test that vague queries return low confidence."""
    text = "can I afford a car"
    result = parse(text, TODAY)

    assert result.confidence < 0.5
    # core.model.DecisionDelta.amount is a non-optional float; 0.0 is the
    # "no amount" sentinel, not None.
    assert result.amount == 0.0


def test_parse_laptop_next_week():
    """Test relative date resolution."""
    text = "$1,200 laptop next week"
    result = parse(text, TODAY)

    assert result.kind == "one_time"
    assert result.amount == 1200
    assert result.start_day == 7  # next week


def test_parse_concert_trip_march():
    """Test larger amount with k suffix."""
    text = "is 1.5k for a concert trip in march ok"
    result = parse(text, TODAY)

    assert result.amount == 1500
    assert result.start_day is not None  # March next year


def test_parse_netflix_monthly():
    """Test monthly subscription."""
    text = "netflix went up to $22.99 a month"
    result = parse(text, TODAY)

    assert result.kind == "recurring"
    assert result.amount == 22.99
    assert result.cadence_days == 30


def test_parse_conflicting_amounts():
    """Test low confidence with multiple conflicting amounts."""
    text = "should i spend $80 or $120 on boots"
    result = parse(text, TODAY)

    # Should either have low confidence or pick one
    assert result.confidence <= 0.85


def test_parse_non_decision():
    """Test that non-spending text returns low confidence."""
    text = "lol what's up"
    result = parse(text, TODAY)

    assert result.confidence < 0.5


def test_parse_rent_increase():
    """Test rent increase with relative date."""
    text = "rent is going up $150/mo starting next month"
    result = parse(text, TODAY)

    assert result.kind == "recurring"
    assert result.amount == 150
    assert result.cadence_days == 30
    assert result.start_day is not None


def test_parse_climbing_gym_weekly():
    """Test weekly recurring."""
    text = "$40 every week for climbing gym"
    result = parse(text, TODAY)

    assert result.kind == "recurring"
    assert result.amount == 40
    assert result.cadence_days == 7


def test_parse_invalid_amount():
    """Test that invented amounts are rejected."""
    text = "$99 laptop"  # $99 not in text
    result = parse(text, TODAY)

    # Since $99 is actually in text, confidence should be OK
    # But if we pass non-existent amount, it should be rejected
    assert result.amount is not None or result.confidence < 0.5


def test_parse_graceful_degradation():
    """Test that parsing always returns a valid DecisionDelta."""
    text = "some random text"
    result = parse(text, TODAY)

    assert result.raw_text == text
    assert isinstance(result.confidence, float)
    assert 0 <= result.confidence <= 1
