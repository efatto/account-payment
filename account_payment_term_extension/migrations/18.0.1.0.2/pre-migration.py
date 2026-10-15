# Copyright 2026 OCA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

"""Repair ``account.payment.term.line`` rows wrongly converted by OpenUpgrade.

The OCA ``account_payment_term_extension`` module historically stored the
extra delay of a payment term line in dedicated integer columns
(``months`` and, above all, ``weeks``).  Odoo 17 refactored
``account.payment.term.line`` around ``delay_type`` + ``nb_days`` and the
OpenUpgrade ``account`` 17.0 post-migration script converts only
``months``/``days``/``end_month``/``days_after``:

    https://github.com/OCA/OpenUpgrade/blob/17.0/openupgrade_scripts/scripts/account/17.0.1.2/post-migration.py

As a consequence:

* the ``weeks`` column (an OCA field) is **completely ignored** and its
  value is lost, so every "N weeks" payment term computes with too few days
  (``nb_days`` ends up without the ``7 * weeks`` part);
* ``months`` is folded into ``30 * months`` days, which drifts on month
  boundaries (e.g. "1 month" from Jan 31st becomes Mar 2nd instead of
  Feb 28th);
* the old ``option`` selection (``after_invoice_month``,
  ``day_following_month``, ...) removed in Odoo 16 was never mapped either.

Odoo never drops the columns of removed fields, so at this point the legacy
``weeks``/``months``/``option``/... columns are still available.  This
pre-migration script runs *after* the ``account`` module has been migrated
(the module depends on it) and rewrites ``delay_type``/``nb_days`` to match
the pre-migration due dates.

It is idempotent: every statement sets absolute values, so running it twice
produces the same result.
"""


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def _columns_exist(cr, table, columns):
    return all(_column_exists(cr, table, column) for column in columns)


def _map_legacy_options(cr):
    """Map the Odoo <=15 ``option`` selection to the new delay types.

    Only the options that can be represented exactly are handled.  The
    ``day_current_month`` option and ``day_of_the_month`` are not
    representable with ``delay_type``/``nb_days`` and are left untouched
    (OpenUpgrade itself considered the latter a lost feature).
    """
    if not _columns_exist(
        cr, "account_payment_term_line", ("option", "delay_type", "days_next_month")
    ):
        return
    # after_invoice_month -> day "days" of the next month
    #   == last day of the invoice month + days
    cr.execute(
        """
        UPDATE account_payment_term_line
        SET delay_type = 'days_after_end_of_month',
            nb_days = COALESCE(days, 0),
            days_next_month = NULL
        WHERE option = 'after_invoice_month'
          AND COALESCE(end_month, FALSE) = FALSE
        """
    )
    # day_following_month -> day "days" of the following month
    cr.execute(
        """
        UPDATE account_payment_term_line
        SET delay_type = 'days_end_of_month_on_the',
            nb_days = 0,
            days_next_month = COALESCE(days, 0)::text
        WHERE option = 'day_following_month'
          AND COALESCE(end_month, FALSE) = FALSE
        """
    )


def _fix_weeks(cr):
    """Fold the dropped OCA ``weeks`` column back into the delay."""
    legacy_columns = (
        "weeks",
        "months",
        "days",
        "end_month",
        "days_after",
        "days_next_month",
    )
    if not _columns_exist(cr, "account_payment_term_line", legacy_columns):
        return
    # Without end of month, the v16 computation was
    #     date_ref + relativedelta(months=m) + relativedelta(weeks=w)
    #               + relativedelta(days=d)
    # * months == 0 -> exact: either "weeks_after" (d == 0) or "days_after"
    #   with 7*w + d days;
    # * months != 0 -> months cannot be mixed with weeks in a single delay
    #   type, fall back to "days_after" with 30*m + 7*w + d days (this is the
    #   same approximation OpenUpgrade already used for months, but at least
    #   the weeks are now taken into account).
    cr.execute(
        """
        UPDATE account_payment_term_line
        SET delay_type = CASE
                WHEN COALESCE(months, 0) = 0 AND COALESCE(days, 0) = 0
                    THEN 'weeks_after'
                ELSE 'days_after'
            END,
            nb_days = CASE
                WHEN COALESCE(months, 0) = 0
                    THEN COALESCE(weeks, 0)
                ELSE COALESCE(months, 0) * 30
                     + COALESCE(weeks, 0) * 7
                     + COALESCE(days, 0)
            END,
            days_next_month = NULL
        WHERE COALESCE(weeks, 0) <> 0
          AND COALESCE(end_month, FALSE) = FALSE
        """
    )
    # end_month + weeks is not exactly representable (v16 applied the end of
    # month *after* the offset, the new delay types apply it *before*).
    # Best effort: "weeks after end of month" when there is nothing else.
    cr.execute(
        """
        UPDATE account_payment_term_line
        SET delay_type = 'weeks_after_end_of_month',
            nb_days = COALESCE(weeks, 0),
            days_next_month = NULL
        WHERE COALESCE(weeks, 0) <> 0
          AND end_month = TRUE
          AND COALESCE(months, 0) = 0
          AND COALESCE(days, 0) = 0
          AND COALESCE(days_after, 0) = 0
        """
    )


def _fix_months(cr):
    """Use the exact ``months_after`` delay type when possible.

    OpenUpgrade converts ``months`` to ``30 * months`` days.  When there is
    no other offset, ``months_after`` computes the calendar-correct date.
    """
    if not _columns_exist(
        cr,
        "account_payment_term_line",
        ("weeks", "months", "days", "end_month", "days_next_month"),
    ):
        return
    cr.execute(
        """
        UPDATE account_payment_term_line
        SET delay_type = 'months_after',
            nb_days = COALESCE(months, 0),
            days_next_month = NULL
        WHERE COALESCE(weeks, 0) = 0
          AND COALESCE(months, 0) <> 0
          AND COALESCE(days, 0) = 0
          AND COALESCE(end_month, FALSE) = FALSE
        """
    )


def _enable_company_delay_types(cr):
    """Make sure the units used by the migrated lines are selectable."""
    if not _column_exists(cr, "res_company", "payment_terms_delay_type"):
        return
    cr.execute(
        """
        UPDATE res_company
        SET payment_terms_delay_type = 'weeks_and_months'
        WHERE payment_terms_delay_type = 'days'
          AND EXISTS (
              SELECT 1
              FROM account_payment_term_line
              WHERE delay_type IN (
                  'weeks_after',
                  'weeks_after_end_of_month',
                  'weeks_after_end_of_next_month',
                  'months_after',
                  'months_after_end_of_month'
              )
          )
        """
    )


def migrate(cr, version):
    if not version:
        return
    if not _column_exists(cr, "account_payment_term_line", "delay_type"):
        # Not migrated to the delay_type/nb_days model yet.
        return
    _map_legacy_options(cr)
    _fix_weeks(cr)
    _fix_months(cr)
    _enable_company_delay_types(cr)

