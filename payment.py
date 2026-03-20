# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from collections import defaultdict
from decimal import Decimal

from trytond.model import ModelView, Workflow, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval, TimeDelta
from trytond.transaction import Transaction
from trytond.wizard import Button, StateTransition, StateView, Wizard

__all__ = [
    'Journal',
    'Payment',
    'BankDebtReconcile',
    'BankDebtReconcileStart',
    ]


class Journal(metaclass=PoolMeta):
    __name__ = 'account.payment.journal'
    processing_account = fields.Many2One('account.account',
        'Processing Account', states={
            'required': Bool(Eval('processing_journal')),
            })
    processing_journal = fields.Many2One('account.journal',
        'Processing Journal', states={
            'required': Bool(Eval('processing_account')),
            })
    bank_debt_account = fields.Many2One('account.account',
        'Bank Debt Account',
        domain=[
            ('company', '=', Eval('company', -1)),
            ('type', '!=', None),
            ('closed', '!=', True),
            ])
    bank_debt_maturity_delay = fields.TimeDelta(
        "Bank Debt Maturity Delay",
        domain=['OR',
            ('bank_debt_maturity_delay', '=', None),
            ('bank_debt_maturity_delay', '>=', TimeDelta()),
            ],
        states={
            'invisible': ~Bool(Eval('bank_debt_account')),
            },
        depends=['bank_debt_account'],
        help="Delay added to the payment maturity date for the bank debt.")

    @classmethod
    def __setup__(cls):
        super().__setup__()
        if hasattr(cls, 'clearing_journal'):
            cls.clearing_journal.context = {'company': Eval('company', -1)}
            cls.clearing_journal.depends.add('company')
        cls.processing_journal.context = {'company': Eval('company', -1)}
        cls.processing_journal.depends.add('company')

    @classmethod
    def cron_reconcile_bank_debt(cls, date=None):
        pool = Pool()
        Date = pool.get('ir.date')
        if date is None:
            date = Date.today()
        journals = cls.search([
                ('company', '=', Transaction().context.get('company')),
                ('bank_debt_account', '!=', None),
                ])
        cls.reconcile_bank_debt(journals, date=date)

    @classmethod
    def reconcile_bank_debt(cls, journals, date=None):
        pool = Pool()
        Date = pool.get('ir.date')
        Line = pool.get('account.move.line')
        Move = pool.get('account.move')
        Period = pool.get('account.period')
        Payment = pool.get('account.payment')

        if date is None:
            date = Date.today()

        moves = []
        to_reconcile = []
        to_reconcile_counterpart = []
        for journal in journals:
            if not journal.bank_debt_account:
                continue
            if not journal.processing_journal:
                continue

            lines = Line.search([
                    ('account', '=', journal.bank_debt_account.id),
                    ('maturity_date', '!=', None),
                    ('maturity_date', '<=', date),
                    ('reconciliation', '=', None),
                    ('move_state', '=', 'posted'),
                    ])
            for line in lines:
                origin = line.move.origin
                if not isinstance(origin, Payment):
                    continue
                payment = origin
                if payment.company != journal.company:
                    continue
                counterpart_account = (
                    journal.processing_account
                    or (payment.line.account if payment.line else None))
                if not counterpart_account:
                    continue

                move_date = line.maturity_date or date
                period = Period.find(journal.company.id, date=move_date)

                move = Move(
                    journal=journal.processing_journal,
                    origin=payment,
                    date=move_date,
                    period=period,
                    company=journal.company)

                balance = line.debit - line.credit
                if not balance:
                    continue
                bank_line = Line()
                bank_line.account = journal.bank_debt_account
                if balance > 0:
                    bank_line.debit, bank_line.credit = 0, balance
                else:
                    bank_line.debit, bank_line.credit = -balance, 0
                bank_line.party = (payment.party
                    if bank_line.account.party_required else None)

                counterpart = Line()
                if balance > 0:
                    counterpart.debit, counterpart.credit = balance, 0
                else:
                    counterpart.debit, counterpart.credit = 0, -balance
                counterpart.account = counterpart_account
                counterpart.party = (payment.party
                    if counterpart.account.party_required else None)

                move.lines = (bank_line, counterpart)
                moves.append(move)

                if bank_line.account.reconcile:
                    to_reconcile.append([line, bank_line])

                if counterpart.account.reconcile:
                    counterpart_lines = [counterpart]
                    if payment.processing_move:
                        if payment.processing_move.state == 'draft':
                            Move.post([payment.processing_move])
                        counterpart_lines += [
                            l for l in payment.processing_move.lines
                            if (l.account == counterpart.account
                                and not l.reconciliation)
                            ]
                    if (payment.line
                            and payment.line.account == counterpart.account
                            and not payment.line.reconciliation):
                        counterpart_lines.append(payment.line)
                    to_reconcile_counterpart.append(counterpart_lines)

        if moves:
            Move.save(moves)
            Move.post(moves)

        for line_objs in to_reconcile:
            lines = Line.browse([l.id for l in line_objs if l.id])
            if not sum(l.debit - l.credit for l in lines):
                Line.reconcile(lines)
        for line_objs in to_reconcile_counterpart:
            lines = Line.browse([l.id for l in line_objs if l.id])
            if not sum(l.debit - l.credit for l in lines):
                Line.reconcile(lines)

class Payment(metaclass=PoolMeta):
    __name__ = 'account.payment'
    processing_move = fields.Many2One('account.move', 'Processing Move',
        readonly=True)

    @classmethod
    @Workflow.transition('processing')
    def process(cls, payments, group):
        pool = Pool()
        Move = pool.get('account.move')

        group = super(Payment, cls).process(payments, group)

        moves = []
        for payment in payments:
            move = payment.create_processing_move()
            if move:
                moves.append(move)
        if moves:
            Move.save(moves)
            cls.write(*sum((([m.origin], {'processing_move': m.id})
                        for m in moves), ()))

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
            if (payment.processing_move
                    and payment.processing_move.state == 'posted'
                    and payment.line
                    and not payment.line.reconciliation):
                lines = [l for l in payment.processing_move.lines
                    if l.account == payment.line.account] + [payment.line]
                if not sum(l.debit - l.credit for l in lines):
                    Line.reconcile(lines)

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
            for lines in list(to_reconcile[party].values()):
                Line.reconcile(lines)

        cls.write(payments, {'processing_move': None})


class BankDebtReconcileStart(ModelView):
    "Bank Debt Reconcile Start"
    __name__ = 'account.payment.journal.bank_debt.reconcile.start'
    date = fields.Date("Date", required=True)

    @staticmethod
    def default_date():
        pool = Pool()
        Date = pool.get('ir.date')
        return Date.today()


class BankDebtReconcile(Wizard):
    "Bank Debt Reconcile"
    __name__ = 'account.payment.journal.bank_debt.reconcile'
    start = StateView(
        'account.payment.journal.bank_debt.reconcile.start',
        'account_payment_processing.bank_debt_reconcile_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Reconcile', 'reconcile', 'tryton-ok', default=True),
            ])
    reconcile = StateTransition()

    def transition_reconcile(self):
        pool = Pool()
        Journal = pool.get('account.payment.journal')
        Journal.reconcile_bank_debt(self.records, date=self.start.date)
        return 'end'
