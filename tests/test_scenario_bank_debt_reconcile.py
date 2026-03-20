import datetime
import unittest
from decimal import Decimal

from proteus import Model, Wizard
from trytond.modules.account.tests.tools import (create_chart,
                                                 create_fiscalyear,
                                                 get_accounts)
from trytond.modules.account_invoice.tests.tools import \
    set_fiscalyear_invoice_sequences
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.tests.test_tryton import drop_db
from trytond.tests.tools import activate_modules


class TestBankDebtReconcile(unittest.TestCase):

    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test(self):

        now = datetime.datetime.now()
        today = now.date()

        activate_modules(
            ['account_payment_processing', 'account_bank_statement_payment'])

        _ = create_company()
        company = get_company()
        tax_identifier = company.party.identifiers.new()
        tax_identifier.type = 'eu_vat'
        tax_identifier.code = 'BE0897290877'
        company.party.save()

        fiscalyear = set_fiscalyear_invoice_sequences(
            create_fiscalyear(company))
        fiscalyear.click('create_period')

        _ = create_chart(company)
        accounts = get_accounts(company)
        receivable = accounts['receivable']
        payable = accounts['payable']
        revenue = accounts['revenue']
        account_cash = accounts['cash']
        account_cash.bank_reconcile = True
        account_cash.reconcile = True
        account_cash.save()

        Account = Model.get('account.account')
        processing_account = Account(
            name='Customers Effects Receivable',
            type=receivable.type,
            bank_reconcile=True,
            reconcile=True,
            party_required=True,
            deferral=True)
        processing_account.save()
        bank_debt_account = Account(
            name='Bank Debt',
            type=payable.type,
            bank_reconcile=True,
            reconcile=True,
            party_required=False,
            deferral=True)
        bank_debt_account.save()

        AccountJournal = Model.get('account.journal')
        bank_journal = AccountJournal(name='Bank Statement', type='cash')
        bank_journal.save()
        revenue_journal, = AccountJournal.find([('code', '=', 'REV')])

        PaymentMethod = Model.get('account.invoice.payment.method')
        payment_method = PaymentMethod()
        payment_method.name = bank_journal.name
        payment_method.company = company
        payment_method.journal = bank_journal
        payment_method.credit_account = account_cash
        payment_method.debit_account = account_cash
        payment_method.save()

        PaymentJournal = Model.get('account.payment.journal')
        payment_journal = PaymentJournal(
            name='Manual receivable with bank debt',
            process_method='manual',
            processing_journal=revenue_journal,
            processing_account=processing_account,
            bank_debt_account=bank_debt_account,
            bank_debt_maturity_delay=datetime.timedelta(days=5))
        payment_journal.save()

        StatementJournal = Model.get('account.bank.statement.journal')
        statement_journal = StatementJournal(name='Test',
                                             journal=bank_journal,
                                             account=account_cash)
        statement_journal.save()

        Party = Model.get('party.party')
        customer = Party(name='Customer')
        customer.save()

        PaymentTerm = Model.get('account.invoice.payment_term')
        payment_term = PaymentTerm(name='Direct')
        payment_term_line = payment_term.lines.new()
        payment_term_line.type = 'remainder'
        payment_term.save()

        Invoice = Model.get('account.invoice')
        customer_invoice = Invoice(type='out')
        customer_invoice.party = customer
        customer_invoice.payment_term = payment_term
        customer_invoice.invoice_date = today
        invoice_line = customer_invoice.lines.new()
        invoice_line.quantity = 1
        invoice_line.unit_price = Decimal('100')
        invoice_line.account = revenue
        invoice_line.description = 'Test'
        customer_invoice.save()
        customer_invoice.click('post')
        self.assertEqual(customer_invoice.state, 'posted')

        Payment = Model.get('account.payment')
        line, = [
            l for l in customer_invoice.move.lines if l.account == receivable
        ]
        pay_line = Wizard('account.move.line.pay', [line])
        pay_line.execute('next_')
        pay_line.form.journal = payment_journal
        pay_line.execute('next_')
        payment, = Payment.find([('state', '=', 'draft')])
        payment.click('submit')
        payment.click('process_wizard')
        payment.reload()
        self.assertEqual(payment.state, 'processing')
        self.assertEqual(payment.processing_move.state, 'draft')
        customer_invoice.reload()
        self.assertEqual(customer_invoice.state, 'posted')

        BankStatement = Model.get('account.bank.statement')
        statement = BankStatement(journal=statement_journal, date=now)
        statement_line = statement.lines.new()
        statement_line.date = now
        statement_line.description = 'Customer Invoice Payment'
        statement_line.amount = Decimal('100.0')
        statement.save()
        statement.click('confirm')
        self.assertEqual(statement.state, 'confirmed')

        statement_line, = statement.lines
        st_move_line = statement_line.lines.new()
        st_move_line.payment = payment
        if not st_move_line.account:
            st_move_line.account = bank_debt_account
        statement_line.save()
        statement_line.click('post')

        payment.reload()
        self.assertEqual(payment.processing_move.state, 'posted')

        bank_debt_line = None
        st_move_line, = statement_line.lines
        st_move_line.reload()
        for move_line in st_move_line.move.lines:
            if move_line.account == bank_debt_account:
                bank_debt_line = move_line
                break
        self.assertNotEqual(bank_debt_line, None)
        expected_maturity = (
            payment.line.maturity_date + datetime.timedelta(days=5))
        self.assertEqual(bank_debt_line.maturity_date, expected_maturity)

        wizard = Wizard(
            'account.payment.journal.bank_debt.reconcile', [payment_journal])
        wizard.form.date = expected_maturity
        wizard.execute('reconcile')

        bank_debt_account.reload()
        processing_account.reload()
        self.assertEqual(bank_debt_account.balance, Decimal('0.00'))
        self.assertEqual(processing_account.balance, Decimal('0.00'))
