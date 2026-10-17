# Payoff

**An offline debt payoff planner for the command line.**

Payoff reads your debts from a plain-text file, simulates every month's payments
under the snowball or avalanche strategy, and tells you when you'll be debt-free
and how much interest you'll pay. It also amortizes single loans, works out the
largest loan a payment can carry, and checks whether refinancing pays for itself.
Everything runs locally, from the Python standard library, with money kept to the cent.

## Features

- **Plain-text debts file.** One debt per line: `name | balance | APR | minimum`. Comments and blank lines are allowed.
- **Line-numbered validation.** Every bad line is reported at once, with the line number and the reason.
- **Snowball and avalanche strategies.** Snowball clears the smallest balance first; avalanche attacks the highest APR first.
- **Monthly extra payments** with `--extra`.
- **One-off lump sums** with `--lump MONTH:AMOUNT` (repeatable).
- **Month-by-month schedule** showing the amount paid, interest charged, remaining balance, and which debts cleared that month.
- **Payoff summary** per debt: starting balance, APR, total interest, and the month it was paid off.
- **Strategy comparison** with `compare`, showing interest and time-to-debt-free for both strategies side by side.
- **Freed minimums roll over.** Once a debt is cleared, its minimum goes to the next debt in line.
- **Exact cents.** All money is `Decimal` rounded half-up to cents, so every schedule ends at exactly `$0.00`.
- **Safety checks.** A minimum that doesn't cover the first month's interest is rejected, since that debt would never shrink. A max-months guard stops runaway plans.
- **Single-loan amortization** with `loan`, using a payment rounded up to the cent so the loan ends on or before its term.
- **Affordability** with `afford`: the largest loan a fixed monthly payment will repay over a given term.
- **Refinance break-even** with `refi`: months until a lower payment repays the closing costs, plus net saving over a horizon.
- **CSV export** of any schedule with `--csv`, one row per debt per month, for a spreadsheet.
- **Start date control** with `--start YYYY-MM-DD`, with month-end dates handled correctly (Jan 31 + 1 month → Feb 28/29).

## Quick start

From the project folder, with the sample debts in `examples/debts.txt`:

```text
# name | balance | APR | minimum payment
Visa         | 4200.00 | 24.99 | 120
Car loan     | 9,800   | 6.5%  | 240
Store card   | 650     | 29    | 35
```

Check the file, then plan the payoff with an extra $150 a month:

```sh
python3 payoff.py check examples/debts.txt
python3 payoff.py plan examples/debts.txt --extra 150 --start 2027-01-01
python3 payoff.py compare examples/debts.txt --extra 150 --start 2027-01-01
```

Add a $1,000 windfall in month 3 and export the schedule:

```sh
python3 payoff.py plan examples/debts.txt --extra 150 --lump 3:1000 --strategy snowball --csv schedule.csv
```

Other commands:

```sh
python3 payoff.py loan --principal 20000 --apr 6.5 --term 60 --start 2027-01-01
python3 payoff.py afford --payment 450 --apr 6.5 --term 60
python3 payoff.py refi --costs 3000 --old 450 --new 400 --horizon 60
```

## How to run

Requires Python 3.9 or newer. There's nothing to install for the program itself.

```sh
python3 payoff.py --help
python3 payoff.py plan --help
```

Debts file rules:

- Lines starting with `#` are comments. Inline `#` comments are not supported, since they would become part of the last field.
- Fields are separated by `|`. Exactly four are required.
- Amounts take digits with up to two decimals, optionally with a `$` and thousands commas (`$9,800.00`). Other comma placements are rejected.
- APR is a percentage, with or without `%` (`24.99` or `24.99%`). Values must be 0 to 99.9999.
- Balance and minimum must be above zero.
- Debt names must be unique (case-insensitive).

Exit codes: `0` on success, `2` when the input is invalid, with each problem printed to stderr as `error: line N: ...`.

## How to test

Tests use `pytest`, configured by `pytest.ini`, and cover the parsers, the simulation rules, the loan math, and the command-line behaviour.

```sh
python3 -m pip install pytest
python3 -m pytest
```

## Project layout

```text
payoff.py            the planner and command-line interface
pytest.ini           test configuration
tests/test_payoff.py unit and command-line tests
examples/debts.txt   sample debts file
```

## Notes on the maths

- Monthly interest is `balance × APR ÷ 12`, rounded half-up to the cent each month.
- Each debt gets its minimum every month. The remaining budget (extra, lump sums, and minimums freed by cleared debts) goes to one debt at a time, in strategy order.
- `afford` and `loan` use the standard annuity formula. A zero APR is handled as an even split.
- `refi` compares monthly payments only. It doesn't account for differences in total interest over the life of the loans.
