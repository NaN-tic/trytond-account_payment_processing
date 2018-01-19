# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from collections import defaultdict
from decimal import Decimal

from trytond.model import ModelView, Workflow, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval
from trytond.transaction import Transaction

__all__ = ['Journal', 'Payment']


class Journal:
    __name__ = 'account.payment.journal'
    __metaclass__ = PoolMeta
    processing_account = fields.Many2One('account.account',
        'Processing Account', states={
            'required': Bool(Eval('processing_journal')),
            },
        depends=['processing_journal'])
    processing_journal = fields.Many2One('account.journal',
        'Processing Journal', states={
            'required': Bool(Eval('processing_account')),
            },
        depends=['processing_account'])


class Payment:
    __name__ = 'account.payment'
    __metaclass__ = PoolMeta
    processing_move = fields.Many2One('account.move', 'Processing Move',
        readonly=True)

    @classmethod
    @Workflow.transition('processing')
    def process(cls, payments, group):
        pool = Pool()
        Move = pool.get('account.move')
        Line = pool.get('account.move.line')

        group = super(Payment, cls).process(payments, group)

        moves = []
        for payment in payments:
            move = payment.create_processing_move()
            if move:
                moves.append(move)
        if moves:
            moves = Move.create([m._save_values for m in moves])
            cls.write(*sum((([m.origin], {'processing_move': m.id})
                        for m in moves), ()))
            Move.post(moves)

        to_reconcile = defaultdict(list)
        for payment in payments:
            if (payment.line
                    and not payment.line.reconciliation
                    and payment.processing_move):
                lines = [l for l in payment.processing_move.lines
                    if l.account == payment.line.account] + [payment.line]
                if not sum(l.debit - l.credit for l in lines):
                    to_reconcile[payment.party].extend(lines)
        for lines in to_reconcile.itervalues():
            Line.reconcile(lines)

        return group

    def create_processing_move(self, date=None):
        pool = Pool()
        Currency = pool.get('currency.currency')
        Move = pool.get('account.move')
        Line = pool.get('account.move.line')
        Period = pool.get('account.period')
        Date = pool.get('ir.date')

        if not self.line:
            return
        if (not self.journal.processing_account
                or not self.journal.processing_journal):
            return
        if self.processing_move:
            return self.processing_move

        if date is None:
            date = Date.today()
        period = Period.find(self.company.id, date=date)

        # compatibility with account_bank_statement_payment
        clearing_percent = getattr(
            self.journal, 'clearing_percent', Decimal(1)) or Decimal(1)
        processing_amount = self.amount * clearing_percent

        local_currency = self.journal.currency == self.company.currency
        if not local_currency:
            with Transaction().set_context(date=self.date):
                local_amount = Currency.compute(
                    self.journal.currency, processing_amount,
                    self.company.currency)
        else:
            local_amount = self.company.currency.round(processing_amount)

        move = Move(
            journal=self.journal.processing_journal,
            origin=self,
            date=date,
            period=period)

        line = Line()
        if self.kind == 'payable':
            line.debit, line.credit = local_amount, 0
        else:
            line.debit, line.credit = 0, local_amount
        line.account = self.line.account
        if not local_currency:
            line.amount_second_currency = processing_amount
            line.second_currency = self.journal.currency
        line.party = (self.line.party
            if self.line.account.party_required else None)

        counterpart = Line()
        if self.kind == 'payable':
            counterpart.debit, counterpart.credit = 0, local_amount
        else:
            counterpart.debit, counterpart.credit = local_amount, 0
        counterpart.account = self.journal.processing_account
        if not local_currency:
            counterpart.amount_second_currency = -processing_amount
            counterpart.second_currency = self.journal.currency
        counterpart.party = (self.line.party
            if self.journal.processing_account.party_required else None)

        move.lines = (line, counterpart)
        return move

    @classmethod
    @ModelView.button
    @Workflow.transition('succeeded')
    def succeed(cls, payments):
        pool = Pool()
        Line = pool.get('account.move.line')

        super(Payment, cls).succeed(payments)

        for payment in payments:
            if (payment.journal.processing_account
                    and payment.journal.processing_account.reconcile
                    and payment.processing_move
                    and payment.journal.clearing_account
                    and payment.journal.clearing_account.reconcile
                    and payment.clearing_move):
                to_reconcile = defaultdict(list)
                lines = (payment.processing_move.lines
                    + payment.clearing_move.lines)
                for line in lines:
                    if line.account.reconcile and not line.reconciliation:
                        key = (
                            line.account.id,
                            line.party.id if line.party else None)
                        to_reconcile[key].append(line)
                for lines in to_reconcile.itervalues():
                    if not sum((l.debit - l.credit) for l in lines):
                        Line.reconcile(lines)

    def create_clearing_move(self, date=None):
        move = super(Payment, self).create_clearing_move(date=date)
        if move and self.processing_move:
            for line in move.lines:
                if line.account == self.line.account:
                    line.account = self.journal.processing_account
                    line.party = (self.line.party
                        if line.account.party_required else None)
        return move

    @classmethod
    @ModelView.button
    @Workflow.transition('failed')
    def fail(cls, payments):
        pool = Pool()
        Move = pool.get('account.move')
        Line = pool.get('account.move.line')
        Reconciliation = pool.get('account.move.reconciliation')

        super(Payment, cls).fail(payments)

        to_delete = []
        to_reconcile = defaultdict(lambda: defaultdict(list))
        to_unreconcile = []
        to_post = []
        for payment in payments:
            if payment.processing_move:
                if (payment.processing_move.state == 'posted'
                        and payment.processing_move.journal.update_posted):
                    Move.draft([payment.processing_move])
                if payment.processing_move.state == 'draft':
                    to_delete.append(payment.processing_move)
                    for line in payment.processing_move.lines:
                        if line.reconciliation:
                            to_unreconcile.append(line.reconciliation)
                else:
                    cancel_move = payment.processing_move.cancel()
                    to_post.append(cancel_move)
                    for line in (payment.processing_move.lines
                            + cancel_move.lines):
                        if line.reconciliation:
                            to_unreconcile.append(line.reconciliation)
                        if line.account.reconcile:
                            to_reconcile[payment.party][line.account].append(
                                line)
        if to_unreconcile:
            Reconciliation.delete(to_unreconcile)
        if to_delete:
            Move.delete(to_delete)
        if to_post:
            Move.post(to_post)
        for party in to_reconcile:
            for lines in to_reconcile[party].itervalues():
                Line.reconcile(lines)

        cls.write(payments, {'processing_move': None})
