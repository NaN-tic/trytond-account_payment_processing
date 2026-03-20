# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.pool import Pool

try:
    from trytond.modules.account_bank_statement_payment.statement import (
        AddPayment as StatementAddPayment,
        StatementMoveLine as StatementMoveLine,
        )
except Exception:  # pragma: no cover - optional module
    StatementMoveLine = None
    StatementAddPayment = None
from . import account
from . import ir
from . import payment
from . import statement


def register():
    Pool.register(
        account.Move,
        ir.Cron,
        payment.Journal,
        payment.Payment,
        payment.BankDebtReconcileStart,
        module='account_payment_processing', type_='model')
    Pool.register(
        payment.BankDebtReconcile,
        depends=['account_payment'],
        module='account_payment_processing', type_='wizard')
    if StatementMoveLine:
        Pool.register_mixin(
            statement.StatementMoveLineMixin,
            StatementMoveLine,
            module='account_payment_processing')
    if StatementAddPayment:
        Pool.register_mixin(
            statement.AddPaymentMixin,
            StatementAddPayment,
            module='account_payment_processing')
