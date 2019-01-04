# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.pool import Pool
from . import payment
from . import statement


def register():
    Pool.register(
        payment.Journal,
        payment.Payment,
        module='account_payment_processing', type_='model')
    Pool.register(
        statement.StatementMoveLine,
        depends='account_bank_statement_payment',
        module='account_payment_processing', type_='model')
