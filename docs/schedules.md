# Schedules

A **schedule** rebuilds a datamodule at set times, so its data is ready before
anyone reads it. A datamodule can have any number of schedules, for example
"every 15 minutes in business hours" plus "06:00 on the first working day".

A due schedule queues a build, and the worker runs it. The worker checks for due
schedules about every 10 seconds. Runs missed while no worker was running collapse
into one, and two workers never run the same due time twice. A scheduled build
always runs, even while automatic rebuilds are backing off after a failure (see
[Freshness and builds](freshness.md#failed-builds)).

## Timezone

Each schedule runs in a named timezone, so "07:00" stays 07:00 local time across
daylight-saving changes. The timezones offered come from the `schedule.timezones`
setting; the first one is the default.

## The kinds of schedule

Pick a kind under **Runs**. **Preview next runs** shows the next five times before
you save.

| Runs | Example | Notes |
|---|---|---|
| Every N minutes or hours | every 15 minutes, Mon–Fri, between 06:00 and 20:00 | The window runs from the start hour up to, but not including, the end hour. No days ticked means every day. |
| Daily at set times | 07:00 and 13:30 | Comma-separated times; optionally only on some days. |
| Every N weeks on chosen days | every 2 weeks on Monday at 07:00 | The start date anchors which weeks count. |
| Monthly on a day of the month | day 31 at 18:00 | Days 29–31 fall back to the month's last day; `last` means the last day. Every N months is allowed. |
| First or last working day of the month | first working day at 06:00 | Working days are Monday to Friday. Public holidays are not yet taken into account. |
| Once | 2027-03-01 at 09:30 | Runs one time and then never again. |
| Custom rule (RRULE) | the second Tuesday of each month at 09:00 | Any recurrence rule (below). |

## Custom rules

Strata stores every schedule as an iCalendar recurrence rule (**RRULE**,
RFC 5545), the format calendar programs use. The builder kinds above write the rule
for you. **Custom rule (RRULE)** takes the rule text directly, so a shape the builder
does not offer still needs no code. Some examples:

| Rule | Runs |
|---|---|
| `FREQ=MONTHLY;BYDAY=TU;BYSETPOS=2;BYHOUR=9;BYMINUTE=0` | 09:00 on the second Tuesday of each month |
| `FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=6;BYMINUTE=30` | 06:30 on Monday, Wednesday and Friday |
| `FREQ=MONTHLY;BYMONTHDAY=-2;BYHOUR=17;BYMINUTE=0` | 17:00 on the second-to-last day of each month |
| `FREQ=YEARLY;BYMONTH=7;BYMONTHDAY=1;BYHOUR=0;BYMINUTE=5` | 00:05 on 1 July (the start of Australia's financial year) |
| `FREQ=MINUTELY;INTERVAL=30;BYDAY=SA,SU;BYHOUR=8,9,10,11` | every 30 minutes from 08:00 to 11:30 at weekends |

Put several rules on separate lines; the schedule runs at every time any of them
gives. The **start date** anchors rules with an interval (such as `INTERVAL=2`).

## Turning a schedule off

Untick **Enabled** to pause a schedule without losing it. A paused schedule has no
next run.
