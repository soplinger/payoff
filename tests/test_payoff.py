from datetime import date
from decimal import Decimal

import pytest

from payoff import (
    Debt,
    DebtFileError,
    PayoffError,
    add_months,
    compare,
    main,
    max_principal,
    monthly_interest,
    parse_debts,
    parse_money,
    parse_rate,
    amortize,
    loan_payment,
    refinance_breakeven,
    simulate,
)

START = date(2027, 1, 1)
D = Decimal


def test_parse_money_accepts_plain_grouped_and_dollar_forms():
    assert parse_money("1200") == D("1200.00")
    assert parse_money("1,200.5") == D("1200.50")
    assert parse_money(" $3,000.00 ") == D("3000.00")


@pytest.mark.parametrize("text", ["12,34", "1.234", "-5", "abc", "", "1e3"])
def test_parse_money_rejects_ambiguous_input(text):
    with pytest.raises(ValueError):
        parse_money(text)


def test_parse_rate_accepts_percent_sign_and_rejects_out_of_range():
    assert parse_rate("24.99") == D("24.99")
    assert parse_rate("6.5%") == D("6.5")
    for text in ["100", "-1", "fast"]:
        with pytest.raises(ValueError):
            parse_rate(text)


def test_parse_debts_skips_blank_lines_and_comments():
    text = "# my debts\n\nVisa | 4200 | 24.99 | 120\n\nCar | 9,800.00 | 6.5% | 240\n"
    debts = parse_debts(text)
    assert [d.name for d in debts] == ["Visa", "Car"]
    assert debts[1].balance == D("9800.00")


def test_parse_debts_reports_every_bad_line_with_its_number():
    text = (
        "Visa | 4200 | 24.99 | 120\n"
        "Card | 12,34 | 19 | 35\n"
        "Loan | 500 | 7\n"
        "Store | 0 | 29 | 20\n"
    )
    with pytest.raises(DebtFileError) as info:
        parse_debts(text)
    errors = info.value.errors
    assert any(e.startswith("line 2: balance:") for e in errors)
    assert any(e == "line 3: expected 4 fields 'name | balance | APR | minimum', found 3" for e in errors)
    assert any(e == "line 4: balance: must be more than zero" for e in errors)


def test_parse_debts_flags_duplicate_names_case_insensitively():
    with pytest.raises(DebtFileError) as info:
        parse_debts("Visa | 100 | 20 | 10\nvisa | 200 | 20 | 10\n")
    assert info.value.errors == ["line 2: name: 'visa' repeats the debt on line 1"]


def test_monthly_interest_rounds_half_up_to_cents():
    assert monthly_interest(D("1000"), D("12")) == D("10.00")
    assert monthly_interest(D("100"), D("1")) == D("0.08")
    assert monthly_interest(D("1000"), D("0")) == D("0.00")


def test_add_months_clamps_to_month_end_and_wraps_years():
    assert add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


def test_schedule_balances_to_the_cent():
    debts = [
        Debt("Visa", D("4200.00"), D("24.99"), D("120.00")),
        Debt("Car", D("9800.00"), D("6.5"), D("240.00")),
        Debt("Store", D("650.00"), D("29"), D("35.00")),
    ]
    plan = simulate(debts, START, extra=D("150.00"))
    principal = sum(d.balance for d in debts)
    assert all(a.paid_month <= plan.months for a in plan.accounts)
    assert plan.total_paid == principal + plan.total_interest
    final_balance = {}
    for row in plan.rows:
        for entry in row.entries:
            final_balance[entry.name] = entry.balance
    assert final_balance == {"Visa": D("0.00"), "Car": D("0.00"), "Store": D("0.00")}


def test_avalanche_sends_extra_to_highest_apr_and_snowball_to_smallest_balance():
    debts = [
        Debt("Small", D("300.00"), D("5"), D("30.00")),
        Debt("Costly", D("900.00"), D("20"), D("40.00")),
    ]
    avalanche = simulate(debts, START, "avalanche", extra=D("100.00"))
    snowball = simulate(debts, START, "snowball", extra=D("100.00"))

    def payment(plan, name):
        return next(e.payment for e in plan.rows[0].entries if e.name == name)

    # Month 1: minimums 30 + 40, pool 170 - 70 = 100 of extra left over.
    assert payment(avalanche, "Costly") == D("140.00")
    assert payment(avalanche, "Small") == D("30.00")
    assert payment(snowball, "Small") == D("130.00")
    assert payment(snowball, "Costly") == D("40.00")
    assert avalanche.total_interest <= snowball.total_interest


def test_cleared_minimums_roll_over_to_remaining_debts():
    debts = [
        Debt("Tiny", D("50.00"), D("0"), D("60.00")),
        Debt("Big", D("1000.00"), D("0"), D("100.00")),
    ]
    plan = simulate(debts, START)
    assert plan.rows[0].entries[0].name == "Tiny"
    month_two_big = next(e for e in plan.rows[1].entries if e.name == "Big")
    assert month_two_big.payment == D("160.00")


def test_lump_sum_finishes_sooner():
    debts = [Debt("Card", D("2000.00"), D("18"), D("60.00"))]
    base = simulate(debts, START)
    lumped = simulate(debts, START, lumps={3: D("800.00")})
    assert lumped.months < base.months
    assert lumped.total_interest < base.total_interest


def test_minimum_that_does_not_cover_interest_is_rejected_by_name():
    debts = [Debt("Payday", D("1000.00"), D("300"), D("20.00"))]
    with pytest.raises(PayoffError, match="Payday: minimum \\$20.00 does not cover"):
        simulate(debts, START)


def test_max_months_guard_stops_very_slow_plans():
    debts = [Debt("Slow", D("10000.00"), D("24"), D("201.00"))]
    with pytest.raises(PayoffError, match="not paid off within 3 months"):
        simulate(debts, START, max_months=3)


def test_compare_returns_both_strategies():
    debts = [Debt("A", D("500"), D("10"), D("50")), Debt("B", D("900"), D("22"), D("60"))]
    plans = compare(debts, START, D("40"))
    assert set(plans) == {"snowball", "avalanche"}


def test_zero_apr_loan_is_even_split():
    assert loan_payment(D("1200"), D("0"), 12) == D("100.00")
    assert max_principal(D("100"), D("0"), 12) == D("1200.00")


def test_amortized_loan_ends_by_term_and_round_trips():
    payment, plan = amortize(D("20000"), D("6.5"), 60, START)
    assert plan.months <= 60
    assert plan.rows[-1].entries[0].balance == D("0.00")
    assert abs(max_principal(payment, D("6.5"), 60) - D("20000")) < D("1")


@pytest.mark.parametrize("principal, apr, term", [("9999.99", "19.99", 37), ("1", "3", 12), ("250000", "0", 360)])
def test_amortized_loan_ends_by_term_for_awkward_inputs(principal, apr, term):
    _, plan = amortize(D(principal), D(apr), term, START)
    assert plan.months <= term
    assert plan.rows[-1].entries[0].balance == D("0.00")


def test_refinance_breakeven_rounds_months_up():
    assert refinance_breakeven(D("3000"), D("450"), D("400"), 60) == (60, D("0.00"))
    months, net = refinance_breakeven(D("3000"), D("450"), D("403"), 60)
    assert months == 64
    assert net == D("-180.00")


def test_refinance_rejects_higher_payment():
    with pytest.raises(PayoffError):
        refinance_breakeven(D("100"), D("400"), D("400"), 12)


def test_cli_check_reports_ok(tmp_path, capsys):
    path = tmp_path / "debts.txt"
    path.write_text("Visa | 4200 | 24.99 | 120\nCar | 9800 | 6.5 | 240\n", encoding="utf-8")
    assert main(["check", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "OK: 2 debts, total balance $14,000.00"


def test_cli_plan_writes_csv(tmp_path, capsys):
    debts = tmp_path / "debts.txt"
    debts.write_text("Visa | 1000 | 20 | 50\n", encoding="utf-8")
    out = tmp_path / "schedule.csv"
    code = main(["plan", str(debts), "--start", "2027-01-01", "--csv", str(out)])
    assert code == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "month,date,debt,interest,payment,balance"
    assert lines[-1].endswith(",0.00")
    assert "Debt-free:" in capsys.readouterr().out


def test_cli_bad_file_exits_2_with_line_numbers(tmp_path, capsys):
    debts = tmp_path / "debts.txt"
    debts.write_text("Visa | lots | 20 | 50\n", encoding="utf-8")
    assert main(["plan", str(debts)]) == 2
    assert "error: line 1: balance:" in capsys.readouterr().err


def test_cli_bad_lump_is_rejected_by_argparse(tmp_path):
    debts = tmp_path / "debts.txt"
    debts.write_text("Visa | 1000 | 20 | 50\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["plan", str(debts), "--lump", "three:500"])
