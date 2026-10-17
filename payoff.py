"""Payoff: an offline debt payoff planner.

Reads a plain-text list of debts, simulates the month-by-month payments under
the snowball or avalanche strategy, and reports payoff dates and interest.
Money is handled as Decimal rounded to cents, so every schedule ends at
exactly 0.00 and the books balance to the cent.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import re
import sys
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")
TWELVE = Decimal("12")
STRATEGIES = ("snowball", "avalanche")
DEFAULT_MAX_MONTHS = 1200

_PLAIN_MONEY = re.compile(r"\d{1,12}(\.\d{1,2})?")
_GROUPED_MONEY = re.compile(r"\d{1,3}(,\d{3})+(\.\d{1,2})?")
_RATE = re.compile(r"\d{1,2}(\.\d{1,4})?")


class PayoffError(ValueError):
    """The requested plan or calculation cannot be done with these inputs."""


class DebtFileError(ValueError):
    """A debts file has one or more invalid lines; every problem is listed."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


@dataclass(frozen=True)
class Debt:
    name: str
    balance: Decimal
    apr: Decimal
    minimum: Decimal


@dataclass
class Entry:
    """One debt's activity in one month."""

    name: str
    interest: Decimal
    payment: Decimal
    balance: Decimal


@dataclass
class MonthRow:
    number: int
    when: date
    entries: list

    @property
    def interest(self):
        return sum((e.interest for e in self.entries), ZERO)

    @property
    def payment(self):
        return sum((e.payment for e in self.entries), ZERO)

    @property
    def balance(self):
        return sum((e.balance for e in self.entries), ZERO)


@dataclass
class AccountResult:
    name: str
    apr: Decimal
    starting_balance: Decimal
    interest: Decimal
    paid_month: int
    paid_date: date


@dataclass
class Plan:
    strategy: str
    start: date
    rows: list
    accounts: list

    @property
    def months(self):
        return len(self.rows)

    @property
    def payoff_date(self):
        return self.rows[-1].when

    @property
    def total_interest(self):
        return sum((a.interest for a in self.accounts), ZERO)

    @property
    def total_paid(self):
        return sum((e.payment for row in self.rows for e in row.entries), ZERO)


# --- parsing -----------------------------------------------------------------


def parse_money(text):
    """Parse '1200', '1,200.50' or '$1200' into cents. Ambiguous forms are rejected."""
    value = text.strip()
    if value.startswith("$"):
        value = value[1:].strip()
    if "," in value:
        if not _GROUPED_MONEY.fullmatch(value):
            raise ValueError(
                f"'{text.strip()}' is not a valid amount (commas must group thousands, e.g. 1,200.00)"
            )
        value = value.replace(",", "")
    if not _PLAIN_MONEY.fullmatch(value):
        raise ValueError(f"'{text.strip()}' is not a valid amount (digits with up to 2 decimals)")
    return Decimal(value).quantize(CENT)


def parse_rate(text):
    """Parse an APR such as '24.99' or '6.5%' into a percentage Decimal."""
    value = text.strip()
    if value.endswith("%"):
        value = value[:-1].strip()
    if not _RATE.fullmatch(value):
        raise ValueError(f"'{text.strip()}' is not a valid APR (a percentage such as 24.99 or 6.5%)")
    return Decimal(value)


def _checked(parser, text, label, problems):
    try:
        return parser(text)
    except ValueError as exc:
        problems.append(f"{label}: {exc}")
        return None


def parse_debts(text):
    """Parse 'name | balance | APR | minimum' lines. Blank lines and '#' comments are skipped."""
    debts = []
    errors = []
    first_line_for = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [part.strip() for part in line.split("|")]
        if len(fields) != 4:
            errors.append(
                f"line {lineno}: expected 4 fields 'name | balance | APR | minimum', found {len(fields)}"
            )
            continue
        name, balance_text, apr_text, minimum_text = fields
        problems = []
        if not name:
            problems.append("name: is empty")
        key = name.casefold()
        if name and key in first_line_for:
            problems.append(f"name: '{name}' repeats the debt on line {first_line_for[key]}")
        balance = _checked(parse_money, balance_text, "balance", problems)
        apr = _checked(parse_rate, apr_text, "APR", problems)
        minimum = _checked(parse_money, minimum_text, "minimum", problems)
        if balance is not None and balance <= 0:
            problems.append("balance: must be more than zero")
        if minimum is not None and minimum <= 0:
            problems.append("minimum: must be more than zero")
        if problems:
            errors.extend(f"line {lineno}: {problem}" for problem in problems)
            continue
        first_line_for[key] = lineno
        debts.append(Debt(name, balance, apr, minimum))
    if errors:
        raise DebtFileError(errors)
    if not debts:
        raise DebtFileError(["no debts found; add lines like 'Visa | 4200 | 24.99 | 120'"])
    return debts


def load_debts(path):
    with open(path, encoding="utf-8") as handle:
        return parse_debts(handle.read())


# --- arithmetic ----------------------------------------------------------------


def money(value):
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def monthly_interest(balance, apr):
    """One month's interest on a balance at an APR given as a percentage."""
    return (balance * apr / (HUNDRED * TWELVE)).quantize(CENT, ROUND_HALF_UP)


def add_months(start, months):
    """Shift a date by whole months, clamping to the end of short months."""
    year, index = divmod(start.month - 1 + months, 12)
    year += start.year
    month = index + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(start.day, last_day))


def _priority(debt, balance, strategy):
    if strategy == "snowball":
        return (balance, debt.name.casefold())
    return (-debt.apr, balance, debt.name.casefold())


# --- simulation ----------------------------------------------------------------


def simulate(
    debts,
    start,
    strategy="avalanche",
    extra=ZERO,
    lumps=None,
    max_months=DEFAULT_MAX_MONTHS,
):
    """Plan month by month. Every debt gets its minimum; the rest of the budget
    (extra, lump sums, and minimums freed by debts already cleared) goes to one
    debt at a time in strategy order."""
    if strategy not in STRATEGIES:
        raise PayoffError(f"strategy must be one of: {', '.join(STRATEGIES)}")
    if not debts:
        raise PayoffError("there are no debts to plan")
    if extra < 0:
        raise PayoffError("extra payment cannot be negative")
    lumps = dict(lumps or {})
    for month, amount in lumps.items():
        if month < 1 or amount <= 0:
            raise PayoffError("lump sums need a month of 1 or later and an amount above zero")

    by_name = {debt.name: debt for debt in debts}
    if len(by_name) != len(debts):
        raise PayoffError("debt names must be unique")
    for debt in debts:
        first = monthly_interest(debt.balance, debt.apr)
        if debt.minimum <= first:
            raise PayoffError(
                f"{debt.name}: minimum {money(debt.minimum)} does not cover the first month's "
                f"interest of {money(first)}, so the balance would never fall"
            )

    names = [debt.name for debt in debts]
    balances = {debt.name: debt.balance for debt in debts}
    interest_total = {name: ZERO for name in names}
    paid = {}
    fixed_budget = sum((debt.minimum for debt in debts), ZERO) + extra
    rows = []
    month = 0
    while any(balances[name] > 0 for name in names):
        month += 1
        if month > max_months:
            raise PayoffError(f"not paid off within {max_months} months; raise the payments")
        when = add_months(start, month - 1)
        open_names = [name for name in names if balances[name] > 0]

        charged = {}
        for name in open_names:
            charge = monthly_interest(balances[name], by_name[name].apr)
            charged[name] = charge
            balances[name] += charge
            interest_total[name] += charge

        pool = fixed_budget + lumps.get(month, ZERO)
        payments = {name: ZERO for name in open_names}
        for name in open_names:
            pay = min(by_name[name].minimum, balances[name])
            payments[name] += pay
            balances[name] -= pay
            pool -= pay

        order = sorted(
            (name for name in open_names if balances[name] > 0),
            key=lambda name: _priority(by_name[name], balances[name], strategy),
        )
        for name in order:
            if pool <= 0:
                break
            pay = min(pool, balances[name])
            payments[name] += pay
            balances[name] -= pay
            pool -= pay

        rows.append(
            MonthRow(
                month,
                when,
                [
                    Entry(name, charged[name], payments[name], balances[name])
                    for name in open_names
                ],
            )
        )
        for name in open_names:
            if balances[name] == 0 and name not in paid:
                paid[name] = (month, when)

    accounts = [
        AccountResult(
            name=debt.name,
            apr=debt.apr,
            starting_balance=debt.balance,
            interest=interest_total[debt.name],
            paid_month=paid[debt.name][0],
            paid_date=paid[debt.name][1],
        )
        for debt in debts
    ]
    return Plan(strategy, start, rows, accounts)


def compare(debts, start, extra=ZERO, lumps=None, max_months=DEFAULT_MAX_MONTHS):
    return {
        strategy: simulate(debts, start, strategy, extra, lumps, max_months)
        for strategy in STRATEGIES
    }


# --- single loans ----------------------------------------------------------------


def _monthly_rate(apr):
    return apr / (HUNDRED * TWELVE)


def loan_payment(principal, apr, months):
    """Level monthly payment, rounded up to the cent so the loan ends on or before term."""
    if principal <= 0:
        raise PayoffError("principal must be more than zero")
    if months < 1:
        raise PayoffError("term must be at least one month")
    rate = _monthly_rate(apr)
    if rate == 0:
        raw = principal / months
    else:
        raw = principal * rate / (1 - (1 + rate) ** (-months))
    return raw.quantize(CENT, ROUND_UP)


def amortize(principal, apr, term, start):
    """Schedule a level-payment loan. The final month's lump pool covers any leftover
    rounding, so the loan always ends by its term; the lump is only paid up to the balance."""
    payment = loan_payment(principal, apr, term)
    debt = Debt("Loan", principal, apr, payment)
    return payment, simulate([debt], start, lumps={term: principal}, max_months=term)


def max_principal(payment, apr, months):
    """Largest loan that a fixed monthly payment fully repays over the term."""
    if payment <= 0:
        raise PayoffError("payment must be more than zero")
    if months < 1:
        raise PayoffError("term must be at least one month")
    rate = _monthly_rate(apr)
    if rate == 0:
        raw = payment * months
    else:
        raw = payment * (1 - (1 + rate) ** (-months)) / rate
    if raw < CENT:
        raise PayoffError("that payment cannot repay even one cent over the term")
    return raw.quantize(CENT, ROUND_DOWN)


def refinance_breakeven(costs, old_payment, new_payment, horizon):
    """Months until a lower payment repays the closing costs, and net saving over horizon months."""
    if costs < 0:
        raise PayoffError("closing costs cannot be negative")
    if new_payment >= old_payment:
        raise PayoffError("the new monthly payment must be lower than the current one")
    saving = old_payment - new_payment
    months = int((costs / saving).to_integral_value(rounding=ROUND_CEILING))
    net = (saving * horizon - costs).quantize(CENT)
    return months, net


# --- output --------------------------------------------------------------------


def format_schedule(plan):
    lines = [f"{'Mo':>4}  {'Date':<10}  {'Paid':>11}  {'Interest':>10}  {'Balance':>12}  Cleared"]
    for row in plan.rows:
        cleared = ", ".join(a.name for a in plan.accounts if a.paid_month == row.number)
        lines.append(
            f"{row.number:>4}  {row.when.isoformat():<10}  {money(row.payment):>11}  "
            f"{money(row.interest):>10}  {money(row.balance):>12}  {cleared}".rstrip()
        )
    return "\n".join(lines)


def format_summary(plan):
    lines = [
        f"{'Debt':<18}{'Balance':>12}{'APR':>9}{'Interest':>12}   Paid off",
    ]
    for account in plan.accounts:
        lines.append(
            f"{account.name:<18}{money(account.starting_balance):>12}"
            f"{account.apr:>8}%{money(account.interest):>12}   "
            f"{account.paid_date.strftime('%Y-%m')} (month {account.paid_month})"
        )
    lines.append("")
    lines.append(f"Strategy:       {plan.strategy}")
    lines.append(f"Debt-free:      {plan.payoff_date.strftime('%Y-%m')} (month {plan.months})")
    lines.append(f"Total paid:     {money(plan.total_paid)}")
    lines.append(f"Total interest: {money(plan.total_interest)}")
    return "\n".join(lines)


def write_csv(plan, path):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["month", "date", "debt", "interest", "payment", "balance"])
        for row in plan.rows:
            for entry in row.entries:
                writer.writerow(
                    [
                        row.number,
                        row.when.isoformat(),
                        entry.name,
                        f"{entry.interest:.2f}",
                        f"{entry.payment:.2f}",
                        f"{entry.balance:.2f}",
                    ]
                )


# --- command line --------------------------------------------------------------


def _money_arg(text):
    try:
        return parse_money(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _rate_arg(text):
    try:
        return parse_rate(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _term_arg(text):
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError("term must be a whole number of months, 1 or more")
    return int(text)


def _date_arg(text):
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must look like 2026-11-01") from exc


def _lump_arg(text):
    month_text, sep, amount_text = text.partition(":")
    if not sep or not month_text.strip().isdigit() or int(month_text) < 1:
        raise argparse.ArgumentTypeError("lump sums look like MONTH:AMOUNT, e.g. 3:500")
    return int(month_text), _money_arg(amount_text)


def _add_schedule_options(parser):
    parser.add_argument("--extra", type=_money_arg, default=ZERO, help="extra amount paid each month")
    parser.add_argument(
        "--lump",
        type=_lump_arg,
        action="append",
        default=[],
        metavar="MONTH:AMOUNT",
        help="one-off extra payment in a given month (repeatable)",
    )
    parser.add_argument("--start", type=_date_arg, default=None, help="first payment month (default: today)")


def build_parser():
    parser = argparse.ArgumentParser(prog="payoff", description="Offline debt payoff planner.")
    sub = parser.add_subparsers(dest="command", required=True)

    plan_p = sub.add_parser("plan", help="simulate a payoff schedule from a debts file")
    plan_p.add_argument("debts", help="plain-text debts file")
    plan_p.add_argument("--strategy", choices=STRATEGIES, default="avalanche")
    plan_p.add_argument("--csv", metavar="PATH", help="also write the schedule as CSV")
    _add_schedule_options(plan_p)

    compare_p = sub.add_parser("compare", help="compare snowball and avalanche side by side")
    compare_p.add_argument("debts", help="plain-text debts file")
    _add_schedule_options(compare_p)

    check_p = sub.add_parser("check", help="validate a debts file without planning")
    check_p.add_argument("debts", help="plain-text debts file")

    loan_p = sub.add_parser("loan", help="amortize a single loan")
    loan_p.add_argument("--principal", required=True, type=_money_arg)
    loan_p.add_argument("--apr", required=True, type=_rate_arg)
    loan_p.add_argument("--term", required=True, type=_term_arg, help="months")
    loan_p.add_argument("--start", type=_date_arg, default=None, help="first payment month (default: today)")
    loan_p.add_argument("--csv", metavar="PATH", help="also write the schedule as CSV")

    afford_p = sub.add_parser("afford", help="largest loan a monthly payment can repay")
    afford_p.add_argument("--payment", required=True, type=_money_arg)
    afford_p.add_argument("--apr", required=True, type=_rate_arg)
    afford_p.add_argument("--term", required=True, type=_term_arg, help="months")

    refi_p = sub.add_parser("refi", help="months for a lower payment to repay closing costs")
    refi_p.add_argument("--costs", required=True, type=_money_arg, help="closing costs")
    refi_p.add_argument("--old", required=True, type=_money_arg, help="current monthly payment")
    refi_p.add_argument("--new", required=True, type=_money_arg, help="new monthly payment")
    refi_p.add_argument("--horizon", type=_term_arg, default=60, help="months to measure net saving over")
    return parser


def _start(args):
    return args.start or date.today()


def _lumps(items):
    lumps = {}
    for month, amount in items:
        lumps[month] = lumps.get(month, ZERO) + amount
    return lumps


def _cmd_plan(args):
    debts = load_debts(args.debts)
    plan = simulate(
        debts,
        _start(args),
        strategy=args.strategy,
        extra=args.extra,
        lumps=_lumps(args.lump),
    )
    print(format_schedule(plan))
    print()
    print(format_summary(plan))
    if args.csv:
        write_csv(plan, args.csv)
        print(f"\nWrote {args.csv}")
    return 0


def _cmd_compare(args):
    debts = load_debts(args.debts)
    plans = compare(debts, _start(args), args.extra, _lumps(args.lump))
    print(f"{'Strategy':<12}{'Months':>8}{'Total interest':>17}{'Debt-free':>12}")
    for strategy in STRATEGIES:
        plan = plans[strategy]
        print(
            f"{strategy:<12}{plan.months:>8}{money(plan.total_interest):>17}"
            f"{plan.payoff_date.strftime('%Y-%m'):>12}"
        )
    snow, aval = plans["snowball"], plans["avalanche"]
    if aval.total_interest < snow.total_interest:
        better, worse = aval, snow
    else:
        better, worse = snow, aval
    saved = worse.total_interest - better.total_interest
    print()
    if saved > 0:
        print(
            f"{better.strategy} pays {money(saved)} less interest than {worse.strategy} "
            f"({better.months} vs {worse.months} months)"
        )
    else:
        print(f"Both strategies cost the same interest ({snow.months} vs {aval.months} months)")
    return 0


def _cmd_check(args):
    debts = load_debts(args.debts)
    total = sum((debt.balance for debt in debts), ZERO)
    print(f"OK: {len(debts)} debts, total balance {money(total)}")
    return 0


def _cmd_loan(args):
    payment, plan = amortize(args.principal, args.apr, args.term, _start(args))
    print(f"Monthly payment: {money(payment)} for {args.term} months")
    print()
    print(format_schedule(plan))
    print()
    print(format_summary(plan))
    if args.csv:
        write_csv(plan, args.csv)
        print(f"\nWrote {args.csv}")
    return 0


def _cmd_afford(args):
    principal = max_principal(args.payment, args.apr, args.term)
    print(
        f"A {money(args.payment)}/month payment at {args.apr}% over {args.term} months "
        f"repays up to {money(principal)}"
    )
    return 0


def _cmd_refi(args):
    months, net = refinance_breakeven(args.costs, args.old, args.new, args.horizon)
    print(f"Monthly saving: {money(args.old - args.new)}")
    print(f"Break-even after {months} month(s)")
    print(f"Net saving over {args.horizon} months: {money(net)}")
    return 0


HANDLERS = {
    "plan": _cmd_plan,
    "compare": _cmd_compare,
    "check": _cmd_check,
    "loan": _cmd_loan,
    "afford": _cmd_afford,
    "refi": _cmd_refi,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except (DebtFileError, PayoffError, OSError) as exc:
        for line in str(exc).splitlines():
            print(f"error: {line}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
