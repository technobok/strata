# Access

Who may open and edit something in Strata is set **per datamodule**. Its reports,
their runs, its checks and its Excel links all follow its datamodule. To show a
wider audience less (an aggregate of payroll, say), build a separate datamodule
on top of it and give that one its own viewers.

## Roles

| Who | What they can do |
|---|---|
| **Admins** (the `auth.admin_group` Gatekeeper group) | Everything, on every datamodule: settings, connections, the SQL console. |
| **Writers** (the `auth.writer_group` group, default `strata-writers`) | Also create datamodules. |
| Everyone else signed in | Whatever each datamodule's lists allow. |

## Owners and viewers

Each datamodule has two lists, set in the **Access** section of its page:

- **Owners** edit it: the build script, sources, dependencies, connections,
  reports, schedules, alerts, API keys and the lists themselves. Owners also view.
- **Viewers** open its reports (as pages and as data), its runs, its checks on the
  Checks page, and its "Use in Excel" box. They can also **Build now** from a
  report page.

Write one entry per line:

- a username as Gatekeeper has it, such as `asiap\jsmith` (case does not matter);
- an `@group`, such as `@brs-finance`;
- `*` for everyone signed in (viewers only: owners are named people or groups).

A ticked **Use the default** box makes a list follow a setting as it changes:
`access.default_owners` (blank = the writers group) and `access.default_viewers`
(default `*`). See [Settings](settings.md#access--apply-immediately). An empty
Owners list means the admins only. An empty Viewers list means the owners only.

When you save, Strata checks the lists:

- you must still be an owner afterwards (admins excepted), so nobody locks
  themselves out by mistake;
- every name must be known to Gatekeeper;
- the new owners must be allowed every input (see the next two sections).

Datamodules made before access control existed are owned by the admins only and
viewed by everyone.

Anyone else signed in gets **"Not allowed"** (HTTP 403) on the datamodule's
pages, reports, runs and data links. The home page lists only the reports you can
open.

## Connections

A connection has a **May use** list (users, `@groups`, `*`; blank = the admins
only), set by an admin on the connection's page. It decides two things:

- who may bind the connection to a datamodule they own;
- that **every owner** of a datamodule bound to it is on the list. This is checked
  when binding, when the owners change, and at every build.

The list controls who may *author* against a connection in Strata. What the
connection can read is decided by its own database login. Give sensitive data its
own connection, with its own login that the general connections cannot use.

## Dependencies

Each owner of a datamodule must be able to read each datamodule it depends on: be
one of the parent's owners or viewers. This is checked:

- when a dependency is added: you must be able to read the parent, and so must
  every other owner ("Not added: … may not read …");
- when the owners change;
- by the script preview, which reads the parents' rows;
- **at every build**, with Gatekeeper's groups as they are at that moment.

A dependant may have **wider viewers** than its parents. That is how an aggregate
reaches more people than the detail behind it. Its owners decide what it shows.

When you narrow a datamodule's lists, Strata warns you about dependants whose
owners could no longer read it. Their next build would lock them.

## Locks

A build stops before reading anything when:

- an owner may no longer read a parent, or use a connection (someone left a group,
  or a list was narrowed), or
- Gatekeeper cannot answer who is in a group.

The build then fails **and the datamodule is locked**. A locked datamodule serves
nothing to anyone, admins included: its report pages and data, its runs, its admin
keys and Excel links, and its scheduled runs. Its alert list gets one mail saying
so.

To unlock it, open its page (it stays open to its owners and the admins, with the
reason). Fix the owners, or get the missing access granted, then build. A
successful build clears the lock. A scheduled build that succeeds clears it too,
for example once Gatekeeper answers again.

A datamodule that depends on a locked one fails to build, because it cannot read
it. It is not locked itself: its owners did not lose access to anything.

## Excel links and keys

A report's Excel link (see [API keys and Excel](api-keys.md)) works for whoever
holds it. That is what it is for, and Strata cannot tell who is using it. When
someone should lose access, an owner **replaces** the report's key on its "Use in
Excel" box, and every workbook with the old link stops refreshing. Only a
datamodule's owners and the admins replace its keys. A lock stops every key.

## Mail from a restricted datamodule

A datamodule is **restricted** when its viewers are not everyone signed in. Its
scheduled run mail carries the count, the data-as-of line and a link to the run,
**not the rows**. Mail bodies are kept in Outbox's shared database, so the rows
stay in Strata, behind its viewers list. Datamodules everyone may read mail the
first 20 rows as usual.

Mail goes to every recipient on a schedule. A recipient who is not a viewer can
open the mail but not the run. The schedule's page and the report's Schedules
table name such recipients. Email addresses cannot be matched to a viewer, so
they are listed as unknown.

## Payroll: a locked-down datamodule

1. An admin creates a connection for the payroll database, **with its own database
   login**. Its May use list names the payroll owners, such as `@payroll-admins`.
2. Make a datamodule such as `finance/payroll`. Set Owners to `@payroll-admins`
   and Viewers to `@payroll`, the people who read it. Bind the connection.
3. For a wider audience, make `finance/payroll-summary` on top of it, with its own
   viewers. Each of its owners must be able to read `finance/payroll`.

The Strata admins, and anyone who can read the server's files, can still see it.
