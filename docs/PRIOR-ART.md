# Prior art and method sources

This file records where each non-trivial method in Observe comes from.
Every method here is taken from long-published, general-purpose sources:
textbook statistics and the documented behaviour of open-source monitoring
tools. None was derived from any vendor's patents, internal documents, or
source code, and no patent text was consulted in writing this code.

**Status of these citations:** the dates, names, and documentation section
titles below were written from general knowledge and have not yet been
checked against the primary sources. Before relying on this file as a
clean-room record, confirm each one and add a URL and an access date.

Keep this file current: when a method is added, record its public source
before or alongside the code.

## Status rollup (`observe/rollup.py`, `observe/scheduler.py`)

**Worst-of group state.** A group shows the most severe state among its
members. This is the display rule of Big Brother (Sean MacGuire, 1996), whose
page colour was the worst colour of any test, and it is the default
aggregation in essentially every dashboard since. The `critical: false`
refinement (a member can degrade its group to WARN but not DOWN) is a
straightforward weighting of that rule.

**Dependency reachability and alert suppression.** A monitor whose parent is
DOWN is reported UNREACHABLE rather than DOWN, and its notifications are
suppressed. This is the host model documented in Nagios Core (originally
NetSaint, Ethan Galstad, 1999): hosts declare `parents`, and the
documentation section "Determining Status and Reachability of Network Hosts"
defines UP, DOWN, and UNREACHABLE on that basis.

**On-demand parent check.** When a child fails, its unconfirmed parents are
polled immediately so the root cause is established before the child alerts.
Nagios Core documents the same behaviour ("on-demand" host checks run when a
dependent service or host changes state).

**Notify on recovery only if a problem was notified.** Nagios Core's
notification logic sends recovery notifications only after a problem
notification; the same rule is used here.

## Capacity forecasting (`observe/forecast.py`)

**Ordinary least squares.** The trend is a least-squares straight line,
published by Adrien-Marie Legendre in 1805 and by Carl Friedrich Gauss in
1809, and present in every introductory statistics text.

**Solving the fitted line for a threshold.** Given value = a + b*t, the
crossing time is t = (threshold - a) / b. Prometheus's `predict_linear()`
function (Prometheus is Apache-2.0 open source) documents the same approach:
simple linear regression over a range vector, extrapolated forward. The
spreadsheet functions `TREND()` and `FORECAST.LINEAR()` do the same.

**Coefficient of determination (r-squared).** Standard goodness-of-fit
measure, reported with every projection and used to label confidence.

**Hourly averaging before fitting.** Simple downsampling to reduce poll-level
noise; RRDtool (Tobias Oetiker, 1999) consolidates samples into averaged
intervals in the same way.

**Not implemented, noted for future work:** Holt-Winters triple exponential
smoothing (Holt 1957, Winters 1960), which handles daily and weekly
seasonality and has been built into RRDtool since 2000.
