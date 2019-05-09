# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from collections import defaultdict
from decimal import Decimal

from trytond.model import fields
from trytond.pool import Pool, PoolMeta

__all__ = ['StatementMoveLine']


class StatementMoveLine(metaclass=PoolMeta):
    __name__ = 'account.bank.statement.move.line'

    @fields.depends('invoice', 'payment')
    def on_change_invoice(self):
        changes = super(StatementMoveLine, self).on_change_invoice()
        if self.invoice and self.payment and self.payment.processing_move:
            # compatibility with account_bank_statement_payment
            clearing_perncent = (
                getattr(self.payment.journal, 'clearing_percent', Decimal(1))
                or Decimal(1))
            if clearing_perncent == Decimal(1):
                for line in self.payment.processing_move.lines:
                    if line.account != self.payment.line.account:
                        changes['account'] = line.account.id
                        changes['account.rec_name'] = line.account.rec_name
                        self.account = line.account
                        break
        return changes

    @fields.depends('payment', 'party', 'account', 'amount',
        '_parent_line._parent_statement.journal',
        methods=['invoice'])
    def on_change_payment(self):
        changes = super(StatementMoveLine, self).on_change_payment()
        if (self.payment and not self.invoice and self.payment.processing_move
                and not self.account):
            for line in self.payment.processing_move.lines:
                if line.account != self.payment.line.account:
                    changes['account'] = line.account.id
                    changes['account.rec_name'] = line.account.rec_name
                    self.account = line.account
                    break
        return changes

    def create_move(self):
        pool = Pool()
        MoveLine = pool.get('account.move.line')

        move = super(StatementMoveLine, self).create_move()

        if (self.payment and self.payment.state == 'succeeded'
                and self.payment.processing_move):
            to_reconcile = defaultdict(list)
            lines = (move.lines + self.payment.processing_move.lines
                + (self.payment.line,))

            if self.payment.clearing_move:
                lines += self.payment.clearing_move.lines

            for line in lines:
                if line.account.reconcile and not line.reconciliation:
                    key = (
                        line.account.id,
                        line.party.id if line.party else None)
                    to_reconcile[key].append(line)
            for lines in list(to_reconcile.values()):
                if not sum((l.debit - l.credit) for l in lines):
                    MoveLine.reconcile(lines)
