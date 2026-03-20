# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from collections import defaultdict
from decimal import Decimal

from trytond.model import fields
from trytond.pool import Pool
from trytond.transaction import Transaction

__all__ = ['StatementMoveLineMixin', 'AddPaymentMixin']


class StatementMoveLineMixin:
    """Mixin for account.bank.statement.move.line."""

    @classmethod
    def write(cls, *args):
        pool = Pool()
        Move = pool.get('account.move')
        MoveLine = pool.get('account.move.line')

        to_post = []
        reconcile_lines = []
        actions = iter(args)
        for lines, values in zip(actions, actions):
            if not values.get('move'):
                continue
            for line in lines:
                payment = getattr(line, 'payment', None)
                if (payment
                        and payment.processing_move
                        and payment.processing_move.state == 'draft'):
                    to_post.append(payment.processing_move)
                reconcile_lines.append(line)

        super().write(*args)

        if to_post:
            Move.post(to_post)

        if reconcile_lines:
            reconcile_lines = cls.browse([l.id for l in reconcile_lines])
            for line in reconcile_lines:
                payment = getattr(line, 'payment', None)
                if not payment or not payment.processing_move:
                    continue
                if not payment.line or not line.move:
                    continue
                if payment.processing_move.state != 'posted':
                    continue
                to_reconcile = defaultdict(list)
                lines = (line.move.lines + payment.processing_move.lines
                    + (payment.line,))
                for rec_line in lines:
                    if (rec_line.account.reconcile
                            and not rec_line.reconciliation):
                        key = (
                            rec_line.account.id,
                            rec_line.party.id if rec_line.party else None)
                        to_reconcile[key].append(rec_line)
                for rec_lines in list(to_reconcile.values()):
                    if not sum((l.debit - l.credit) for l in rec_lines):
                        MoveLine.reconcile(rec_lines)

    def _get_payment_account(self, payment):
        journal = payment.journal
        if getattr(journal, 'bank_debt_account', None):
            return journal.bank_debt_account
        if payment.processing_move:
            if payment.line:
                for line in payment.processing_move.lines:
                    if line.account != payment.line.account:
                        return line.account
            elif payment.processing_move.lines:
                return payment.processing_move.lines[0].account
        if payment.line:
            return payment.line.account
        return None

    def _get_allowed_payment_accounts(self, payment):
        journal = payment.journal
        if getattr(journal, 'bank_debt_account', None):
            return {journal.bank_debt_account}
        accounts = set()
        if payment.processing_move:
            if payment.line:
                for line in payment.processing_move.lines:
                    if line.account != payment.line.account:
                        accounts.add(line.account)
            else:
                accounts.update(
                    line.account for line in payment.processing_move.lines)
        if payment.line:
            accounts.add(payment.line.account)
        return accounts

    def _get_bank_debt_maturity_date(self, payment):
        maturity_date = None
        if payment.line and payment.line.maturity_date:
            maturity_date = payment.line.maturity_date
        elif payment.date:
            maturity_date = payment.date
        delay = payment.journal.bank_debt_maturity_delay
        if maturity_date and delay:
            maturity_date += delay
        return maturity_date

    @fields.depends('invoice', 'payment')
    def on_change_invoice(self):
        super().on_change_invoice()
        if self.invoice and self.payment:
            # compatibility with account_bank_statement_payment
            clearing_percent = (
                getattr(self.payment.journal, 'clearing_percent', Decimal(1))
                or Decimal(1))
            if clearing_percent == Decimal(1):
                account = self._get_payment_account(self.payment)
                if account and (not self.account
                        or self.payment.journal.bank_debt_account):
                    self.account = account
        return

    @fields.depends('payment', 'party', 'account', 'amount',
        '_parent_line._parent_statement.journal',
        methods=['invoice'])
    def on_change_payment(self):
        super().on_change_payment()
        if self.payment and not self.invoice:
            account = self._get_payment_account(self.payment)
            if (account
                    and (not self.account
                        or self.payment.journal.bank_debt_account)):
                self.account = account
        return

    @fields.depends('account', 'payment')
    def on_change_account(self):
        original_payment = self.payment
        super().on_change_account()
        if original_payment and not self.payment:
            if self.account in self._get_allowed_payment_accounts(
                    original_payment):
                self.payment = original_payment

    def _get_move_lines(self):
        move_lines = super()._get_move_lines()
        if (self.payment
                and self.payment.journal.bank_debt_account
                and self.account == self.payment.journal.bank_debt_account):
            maturity_date = self._get_bank_debt_maturity_date(self.payment)
            if maturity_date:
                for line in move_lines:
                    if line.account == self.payment.journal.bank_debt_account:
                        line.maturity_date = maturity_date
                        break
        return move_lines


class AddPaymentMixin:
    """Mixin for account.bank.statement.payment.add."""

    def transition_add(self):
        pool = Pool()
        StatementLine = pool.get('account.bank.statement.line')
        BSMoveLine = pool.get('account.bank.statement.move.line')

        payments = self.start.payments

        to_create = []
        for line in StatementLine.browse(Transaction().context['active_ids']):
            for payment in payments:
                if payment.journal.bank_debt_account:
                    account = payment.journal.bank_debt_account
                elif payment.processing_move:
                    account = None
                    if payment.line:
                        for move_line in payment.processing_move.lines:
                            if move_line.account != payment.line.account:
                                account = move_line.account
                                break
                    if not account and payment.processing_move.lines:
                        account = payment.processing_move.lines[0].account
                else:
                    if payment.line and payment.line.account:
                        account = payment.line.account
                    elif payment.kind == 'payable':
                        if not payment.party.account_payable:
                            continue
                        account = payment.party.account_payable
                    elif payment.kind == 'receivable':
                        if not payment.party.account_receivable:
                            continue
                        account = payment.party.account_receivable
                if not account:
                    continue

                bsmove_line = BSMoveLine()
                bsmove_line.line = line
                bsmove_line.payment = payment
                bsmove_line.invoice = None
                bsmove_line.on_change_payment()
                bsmove_line.date = line.date.date()
                bsmove_line.amount = bsmove_line.amount or payment.amount
                bsmove_line.party = bsmove_line.party or payment.party
                bsmove_line.account = bsmove_line.account or account
                bsmove_line.description = payment.reference
                to_create.append(bsmove_line._save_values())

        if to_create:
            BSMoveLine.create(to_create)

        return 'end'
